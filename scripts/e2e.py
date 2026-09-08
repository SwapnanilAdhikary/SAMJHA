"""End-to-end flow driver. Point it at ANY KFS and it walks the whole product.

    make serve                                   # in another terminal, first

    uv run python scripts/e2e.py                        # a fixture, upload + links only
    uv run python scripts/e2e.py --drive                # ...and simulate the call
    uv run python scripts/e2e.py --doc ~/my_kfs.docx --drive
    uv run python scripts/e2e.py --sweep                # every fixture, both formats
    uv run python scripts/e2e.py --doc x.pdf --supply "Cooling-off period (days)=3"

Nothing here is hardcoded to a fixture index. The document is an argument, the clauses come
from whatever that document actually says, and the clause the barge-in lands on is CHOSEN
from the document's own key values — so pointing this at a real lender's KFS exercises the
same path as pointing it at a specimen.

`--drive` SPEAKS THROUGH RIME. Every clause of the uploaded document is synthesized by
Rime Coda (`coda`/`taru`/`hi`, mu-law 8 kHz, `segment=never` + one explicit flush per
segment) over `wss://users-ws.rime.ai/ws3`, exactly as the agent does it. That matters for
two reasons beyond demonstrating the vendor:

  * **The durations are real.** mu-law at 8 kHz is one byte per sample, so a segment's
    length in bytes IS its length in seconds, exactly. Heard-through accounting, and
    therefore the whole consent mechanic, is arithmetic over Rime's own byte counts rather
    than over an estimate.
  * **The barge-in lands where it really would.** The cut point is computed from the
    measured end of the segment carrying the key value, so "she did not hear the account
    number" is a fact about audio that was actually produced.

`--play` plays it out of the speakers; `--save DIR` writes per-clause WAVs to listen to.

What is NOT real here: there is no LiveKit room and no microphone, so playout is simulated
against the measured durations rather than reported by `PlaybackFinishedEvent`, and
teach-back is not graded by an LLM against speech. Everything that decides an OUTCOME is
still real code — `kfs.build_clauses` builds the clauses, `agent.consent_fsm.ConsentFSM`
runs the state machine, `agent.rushed_consent.evaluate` returns the verdict, and events
land in `events/{call_id}.jsonl` for the API to tail. The script never writes a verdict of
its own, which is why its assertions mean something.

`--fake-audio` skips Rime entirely, sizing segments from their text length. Use it for
offline work and CI; it makes the durations estimates rather than measurements, and the
run says so on screen and in the call's event log.

For a call with a real microphone and real ASR, use `make talk` (terminal) or the browser
flow (`make serve` + `make agent`).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.consent_fsm import ConsentFSM, jsonl_sink  # noqa: E402
from agent.rushed_consent import evaluate  # noqa: E402
from api import events  # noqa: E402
from kfs.build_clauses import build_clauses  # noqa: E402
from kfs.clauses import Clause, ClauseState  # noqa: E402
from kfs.schema import KFS  # noqa: E402

DIM, BOLD, GREEN, RED, YELLOW, BLUE, RESET = (
    "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[0m")

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures/synthetic"

# mu-law at 8 kHz is 1 byte per sample, so bytes = seconds x 8000. Rime's real rate for
# these clauses came out near this; it is a plausible stand-in, NOT a measurement, and it
# is the only thing --drive invents.
SPEAKING_RATE = 13.0     # Devanagari characters per second
MIN_SEGMENT_S = 0.55
SAMPLE_RATE = 8000

# How long a simulated borrower takes to answer. Above rushed_consent.MIN_DELIBERATION_S
# so a clean run is GRANTED and a rushed one is not — both are real decisions either way.
DELIBERATE_S = 3.0
RUSHED_S = 0.4


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}✗ {msg}{RESET}")


def info(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}")


def head(msg: str) -> None:
    print(f"\n{BOLD}{msg}{RESET}")


class Failed(RuntimeError):
    pass


# --------------------------------------------------------------------------- api


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.c = httpx.Client(timeout=30.0)

    def check(self) -> dict:
        try:
            h = self.c.get(f"{self.base}/health").json()
        except httpx.RequestError as e:
            raise Failed(
                f"no API at {self.base} ({type(e).__name__}). Start it with `make serve`."
            ) from None

        # Write events WHERE THE SERVER READS THEM, whatever that is. This script appends
        # to events/{call_id}.jsonl and the API tails it, so a mismatched
        # SAMJHA_EVENTS_DIR means everything appears to work and nothing ever reaches the
        # panel or the record — silently, because a missing file is indistinguishable
        # from a call that has not started. Asking the server removes the footgun instead
        # of documenting it.
        server_dir = Path(h["events"]).resolve()
        local_dir = Path(os.environ.get("SAMJHA_EVENTS_DIR", "events")).resolve()
        if server_dir != local_dir:
            info(f"events dir: following the server to {server_dir}")
        os.environ["SAMJHA_EVENTS_DIR"] = str(server_dir)
        return h

    def upload(self, path: Path, *, role: str, synthetic: bool) -> dict:
        params = {"filename": path.name, "role": role,
                  "synthetic": "true" if synthetic else "false"}
        r = self.c.post(f"{self.base}/kfs", params=params, content=path.read_bytes())
        if r.status_code != 200:
            raise Failed(f"upload refused ({r.status_code}): {r.json().get('detail', r.text)}")
        return r.json()

    def supply(self, doc_id: str, values: dict[str, str]) -> dict:
        r = self.c.patch(f"{self.base}/kfs/{doc_id}",
                         json={"values": values, "by": "scripts/e2e.py"})
        if r.status_code != 200:
            raise Failed(f"correction refused ({r.status_code}): {r.json().get('detail')}")
        return r.json()

    def create_call(self, doc_id: str) -> dict:
        r = self.c.post(f"{self.base}/calls", json={"doc_id": doc_id})
        if r.status_code != 200:
            raise Failed(f"call refused ({r.status_code}): {r.json().get('detail')}")
        return r.json()

    def kfs_for(self, call_id: str) -> dict:
        r = self.c.get(f"{self.base}/calls/{call_id}/kfs")
        if r.status_code != 200:
            raise Failed(f"no KFS for {call_id} ({r.status_code})")
        return r.json()

    def record(self, call_id: str) -> dict:
        return self.c.get(f"{self.base}/calls/{call_id}/record").json()


# ------------------------------------------------------------------- simulation


async def speak_clauses(clauses: list[Clause], *, save: Path | None) -> float:
    """Synthesize every clause through Rime. Returns total seconds of audio produced.

    One text per flush, `key_value_flags` passed through, which is the delivery contract
    the whole product rests on: at most one comprehension-critical value per flush
    segment, so "did she hear the APR?" reduces to "did segment k finish playing?".

    Durations come back exact — mu-law at 8 kHz is 1 byte per sample.
    """
    from delivery import rime_ws3  # noqa: PLC0415  (needs RIME_API_KEY)

    if save:
        save.mkdir(parents=True, exist_ok=True)

    total = 0.0
    for clause in clauses:
        started = time.monotonic()
        try:
            result = await rime_ws3.synthesize(
                [s.text for s in clause.segments],
                key_value_flags=[s.carries_key_value for s in clause.segments])
        except Exception as e:  # noqa: BLE001 — surface the vendor's own message
            raise Failed(
                f"Rime failed on clause {clause.id!r}: {type(e).__name__}: {e}\n"
                f"      Rime is US-only with no India region, so a trans-Pacific connect "
                f"can time out; rime_ws3 already retries 3x. Check RIME_API_KEY, or run "
                f"with --fake-audio to work offline."
            ) from None

        for seg, out in zip(clause.segments, result.segments):
            seg.audio = bytes(out.audio)

        elapsed = time.monotonic() - started
        total += clause.total_duration_s
        if save:
            _write_wav(save / f"{clause.id}.wav", result.audio)
        info(f"{clause.id:<18} {clause.total_duration_s:5.1f}s audio  "
             f"{len(clause.segments):>2} flush segments  synthesized in {elapsed:4.1f}s")

    return total


def _write_wav(path: Path, ulaw: bytes) -> None:
    """mu-law -> an 8 kHz 16-bit PCM WAV, so it can actually be listened to."""
    import wave  # noqa: PLC0415

    from agent.session import ulaw_to_pcm16  # noqa: PLC0415

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(ulaw_to_pcm16(ulaw))


def play(ulaw: bytes, *, upto_s: float | None = None) -> None:
    """Play mu-law out of the speakers, optionally cut short at `upto_s`.

    Cutting the playback at the barge-in point is not cosmetic: hearing the account number
    get chopped mid-digit is the demo.
    """
    import numpy as np  # noqa: PLC0415
    import sounddevice as sd  # noqa: PLC0415

    from agent.session import ulaw_to_pcm16  # noqa: PLC0415

    if upto_s is not None:
        ulaw = ulaw[: int(upto_s * SAMPLE_RATE)]
    pcm = np.frombuffer(ulaw_to_pcm16(ulaw), dtype="<i2").astype(np.float32) / 32768.0
    sd.play(pcm, SAMPLE_RATE)
    sd.wait()


def give_synthetic_audio(clauses: list[Clause]) -> None:
    """Size each segment from its text length. --fake-audio only.

    These are ESTIMATES, not measurements. The run says so on screen and writes it into
    the call's event log, because a duration in a consent record that looks measured and
    is not would be exactly the kind of quiet fabrication this project exists to avoid.
    """
    for clause in clauses:
        for seg in clause.segments:
            seconds = max(MIN_SEGMENT_S, len(seg.text) / SPEAKING_RATE)
            seg.audio = b"\x00" * int(seconds * SAMPLE_RATE)


def pick_barge_in(clauses: list[Clause], want: str) -> Clause | None:
    """Which clause to interrupt, chosen from the document rather than hardcoded.

    Prefers a clause carrying an `account_identifier`: that is the category the day-1
    pilot found Rime reads as a quantity rather than a digit sequence, so it is the
    clause the product's central claim is about. Falls back to any clause with a value.
    """
    if want == "none":
        return None
    if want != "auto":
        found = next((c for c in clauses if c.id == want), None)
        if found is None:
            raise Failed(f"no clause {want!r}. Have: {', '.join(c.id for c in clauses)}")
        if not found.key_values:
            raise Failed(f"clause {want!r} carries no key value to be cut off mid-way")
        return found

    for kind in ("account_identifier", "percentage_apr", "rupee_amount"):
        found = next((c for c in clauses if any(kv.kind == kind for kv in c.key_values)), None)
        if found is not None:
            return found
    return next((c for c in clauses if c.key_values), None)


def deliver(fsm: ConsentFSM, call_id: str, clause: Clause, *, played_s: float,
            interrupted: bool, steps: int = 6, pace: float = 0.0) -> ClauseState:
    """Play a clause to `played_s`, emitting progress so a watching UI animates.

    `pace` is the wall-clock seconds to spread those steps over. Pass the clause's real
    audio duration and the on-screen progress tracks the audio exactly — which is what
    makes a screen recording muxable with the captured Rime track.
    """
    fsm.begin_delivery(clause.id)

    # Enough steps that the heard-through bar moves smoothly rather than in jumps, but
    # not so many that the event log is mostly progress noise.
    if pace:
        steps = max(steps, int(pace * 4))

    for i in range(1, steps + 1):
        clause.heard_through_s = played_s * i / steps
        events.emit(call_id, "clause_state", active=True, reason="deliver",
                    **events.clause_payload(clause))
        if pace:
            time.sleep(pace / steps)

    state = fsm.end_delivery(clause.id, played_s=played_s, interrupted=interrupted)
    clause.heard_through_s = played_s
    clause.state = state
    events.emit(call_id, "clause_state", active=False,
                reason="barge-in mid-number" if interrupted else "delivered",
                **events.clause_payload(clause))
    return state


def teach_back(fsm: ConsentFSM, call_id: str, clause: Clause, *, passed: bool) -> None:
    """A teach-back attempt, graded by the FSM. The transcript is illustrative only.

    No LLM and no ASR here: `agent/teachback.py` is exercised by `make talk` and by the
    test suite. What this drives is the FSM's response to a pass or a fail.
    """
    spoken = " ".join(kv.spoken_text for kv in clause.heard_key_values()) or "समझ गया"
    transcript = spoken if passed else "पता नहीं"
    fsm.teach_back(clause.id, transcript=transcript, passed=passed,
                   grades=[{"fact_id": kv.kind, "verdict": "understood" if passed else "wrong"}
                           for kv in clause.key_values])
    clause.state = fsm.state(clause.id)
    events.emit(call_id, "clause_state", active=False,
                reason="teach_back_pass" if passed else "teach_back_fail",
                **events.clause_payload(clause))
    events.emit(call_id, "teach_back", clause_id=clause.id, attempt=1,
                transcript=transcript, passed=passed,
                grade="pass" if passed else "fail")


async def prepare(api: Api, call: dict, *, fake_audio: bool,
                  save: Path | None) -> tuple[list[Clause], str]:
    """Build this call's clauses and synthesize them. Returns (clauses, provenance).

    Split out from `drive` so a caller can pay the synthesis cost BEFORE anything is
    watching. Synthesizing ten clauses takes about a minute, and during it no events are
    written at all — long enough that a UI waiting on the event stream concludes no agent
    ever joined. `scripts/record_demo.py` calls this before it opens the browser.
    """
    body = api.kfs_for(call["id"])
    kfs = KFS.model_validate(body["kfs"])
    clauses = build_clauses(kfs)

    if fake_audio:
        give_synthetic_audio(clauses)
        print(f"  {YELLOW}--fake-audio: durations are estimates, not measurements{RESET}")
        return clauses, (
            "Audio was NOT synthesized (--fake-audio): segment durations are estimated "
            "from text length and are NOT measurements."
        )

    print(f"  synthesizing through Rime  {DIM}coda/taru/hi · mu-law 8 kHz · /ws3{RESET}")
    seconds = await speak_clauses(clauses, save=save)
    ok(f"{seconds:.1f}s of real Rime audio" + (f", WAVs in {save}" if save else ""))
    return clauses, (
        f"Audio synthesized by Rime coda/taru/hi over /ws3 at mu-law 8 kHz: "
        f"{seconds:.1f}s across {sum(len(c.segments) for c in clauses)} flush segments. "
        f"Durations are byte-exact (1 byte = 1 sample at 8 kHz)."
    )


def deliver_call(call: dict, doc: dict, clauses: list[Clause], provenance: str, *,
                 barge: str, rushed: bool, pace: float, do_play: bool,
                 realtime: bool = False) -> tuple[object, bool]:
    """Register, deliver, decide. Synchronous — the audio is already in hand.

    Returns (the verdict it produced, whether a clause was actually interrupted). The
    second value matters because a refusal from a barge-in and a refusal from the
    deliberation floor are different findings, and only the first should leave a clause
    PARTIALLY_HEARD.
    """
    call_id = call["id"]
    fsm = ConsentFSM(clauses, call_id=call_id, sink=jsonl_sink(call_id))

    events.emit(call_id, "note",
                text=f"scripts/e2e.py driving {doc['filename'] or doc['doc_id']} "
                     f"(sha256 {doc['sha256'][:12]}…). {provenance} There is no LiveKit "
                     f"room and no microphone, so playout is simulated against those "
                     f"durations rather than reported by PlaybackFinishedEvent, and "
                     f"teach-back is not ASR-graded. The state machine and the consent "
                     f"verdict are the real ones.")
    for ordinal, clause in enumerate(clauses):
        events.emit(call_id, "clause_registered", ordinal=ordinal,
                    **events.clause_payload(clause))
    ok(f"registered {len(clauses)} clauses: {', '.join(c.id for c in clauses)}")

    target = pick_barge_in(clauses, barge)
    if target is not None:
        kv_index = next(i for i, s in enumerate(target.segments) if s.key_value)
        cut_at = max(0.0, target.segment_end_time(kv_index) - 0.7)
        kv = target.segments[kv_index].key_value
        info(f"will cut {target.id} at {cut_at:.1f}s of {target.total_duration_s:.1f}s "
             f"— 0.7s before {kv.kind} {kv.raw_text!r} finishes")

    for clause in clauses:
        audio = b"".join(s.audio for s in clause.segments)

        if clause is target:
            if do_play:
                # Cut the playback where the barge-in cuts it. Hearing the number get
                # chopped mid-digit is the demo.
                play(audio, upto_s=cut_at)
            state = deliver(fsm, call_id, clause, played_s=cut_at, interrupted=True,
                            pace=cut_at if realtime else pace)
            heard = [kv.raw_text for kv in clause.heard_key_values()]
            missed = [kv.raw_text for kv in clause.key_values if kv not in clause.heard_key_values()]
            (ok if state is ClauseState.PARTIALLY_HEARD else bad)(
                f"{clause.id:<18} {state.value:<16} heard {heard} missed {missed}")
            # PARTIALLY_HEARD is a trap door: no teach-back, re-read from the start.
            continue

        if do_play:
            play(audio)
        deliver(fsm, call_id, clause, played_s=clause.total_duration_s,
                interrupted=False,
                pace=clause.total_duration_s if realtime else pace)
        teach_back(fsm, call_id, clause, passed=True)
        state = fsm.state(clause.id)
        (ok if state is ClauseState.UNDERSTOOD else bad)(
            f"{clause.id:<18} {state.value}")

    utterance = "हाँ हाँ ठीक है, बस करो" if rushed else "हाँ, मैं सहमत हूँ"
    latency = RUSHED_S if rushed else DELIBERATE_S
    info(f"borrower says {utterance!r} after {latency}s")
    decision = evaluate(fsm, utterance, latency_s=latency)
    events.emit(call_id, "call_end")

    colour = GREEN if decision.granted else YELLOW
    print(f"  {colour}{BOLD}CONSENT {'GRANTED' if decision.granted else 'REFUSED'}{RESET}")
    for reason in decision.reasons:
        print(f"      {YELLOW}{reason}{RESET}")
    return decision, target is not None


async def drive(api: Api, call: dict, doc: dict, *, barge: str, rushed: bool, pace: float,
                fake_audio: bool, do_play: bool, save: Path | None,
                realtime: bool = False) -> tuple[object, bool]:
    """Synthesize, then take the call. The two halves are separable; see `prepare`."""
    clauses, provenance = await prepare(api, call, fake_audio=fake_audio, save=save)
    return deliver_call(call, doc, clauses, provenance, barge=barge, rushed=rushed,
                        pace=pace, do_play=do_play, realtime=realtime)


# ------------------------------------------------------------------- assertions


def verify(api: Api, call: dict, doc: dict, *, granted: bool, barged: bool,
           n_clauses: int) -> int:
    """Check the sealed record against what the run actually produced."""
    head("the sealed consent record")
    sealed = api.record(call["id"])
    rec = sealed["record"]
    failures = 0

    def check(cond: bool, msg: str) -> None:
        nonlocal failures
        if cond:
            ok(msg)
        else:
            bad(msg)
            failures += 1

    from api import records as records_mod

    # FIRST, and not a formality: `all(...)` over an empty list is True, so without this
    # a record that folded nothing at all would pass most of the checks below and look
    # green. Every "every clause ..." assertion depends on this one.
    check(len(rec["clauses"]) == n_clauses,
          f"all {n_clauses} clauses reached the record (got {len(rec['clauses'])})")
    check(bool(rec["consent_decisions"]),
          f"the consent decision reached the record ({len(rec['consent_decisions'])})")

    check(sealed["hash"] == records_mod.record_hash(rec), "hash matches its payload")
    p = rec["kfs_provenance"]
    check(p["sha256"] == doc["sha256"],
          f"provenance cites the uploaded bytes ({p['sha256'][:12]}…)")
    check(p["method"] == doc["method"], f"read by {p['method']}")
    check(rec["synthetic_data"] is doc["synthetic"],
          f"synthetic_data={rec['synthetic_data']} matches the uploader's declaration")
    check(call["fallback_allowed"] == 0,
          "the demo fixture can never replay over this call")

    if granted:
        check(rec["consent"]["decision"] == "GRANTED", "consent GRANTED")
        check(rec["consent_permitted"] is True, "consent permitted")
        check(not rec["clauses_blocking_consent"], "no clause blocks consent")
        check(all(c["final_state"] == "UNDERSTOOD" for c in rec["clauses"]),
              "every clause reached UNDERSTOOD")
        return failures

    check(rec["consent"]["decision"] == "REFUSED", "consent REFUSED")
    check(bool(rec["refusals"]), "the refusal is a row in the record")

    partial = [c for c in rec["clauses"] if c["final_state"] == "PARTIALLY_HEARD"]
    if barged:
        # The ledger itself blocks: a value was never heard, so consent is not open.
        check(rec["consent_permitted"] is False, "the clause ledger does not permit consent")
        # Refused because a value was cut off. There must be a blocking clause, and its
        # value must be recorded as NOT heard.
        blocking = rec["clauses_blocking_consent"]
        check(bool(blocking), f"blocked on {len(blocking)} clause(s): {blocking}")
        check(bool(partial), "a clause is PARTIALLY_HEARD")
        for c in partial:
            check(bool(c["key_values_not_heard"]),
                  f"{c['clause_id']}: {[k['raw_text'] for k in c['key_values_not_heard']]} "
                  f"NOT heard at {c['heard_through_pct']}% of the clause")
    else:
        # Refused on the DELIBERATION FLOOR alone: every clause was understood and she
        # still answered too fast.
        check(not partial, "no clause is PARTIALLY_HEARD — she heard all of it")
        check(not rec["clauses_blocking_consent"],
              "no clause blocks consent; the refusal is the rushed-consent gate alone")
        # THE DIVERGENCE IS THE FINDING, and the two fields must not be conflated:
        # `consent_permitted` is the clause LEDGER's verdict ("everything was heard and
        # understood"), while `consent.decision` is the FINAL one. Here the ledger says
        # yes and the gate still says no, which is the whole point of having a gate — a
        # borrower can have heard every word and still not have had time to consider it.
        check(rec["consent_permitted"] is True,
              "the ledger permits consent, and the gate refused anyway")
        check(any("faster_than_deliberation_floor" in (r.get("reason") or "")
                  for r in rec["refusals"]),
              "the record says WHY: faster than the deliberation floor")
        check(rec["consent"]["flagged_for_callback"] is True,
              "flagged for a human callback")

    return failures


# ------------------------------------------------------------------------ flow


def default_doc() -> Path:
    """A fixture, chosen at random across BOTH formats so a run is never format-blind."""
    candidates = sorted(FIXTURES.glob("pdf/*.pdf")) + sorted(FIXTURES.glob("docx/*.docx"))
    if not candidates:
        raise Failed("no fixtures — run `make fixtures`")
    return random.choice(candidates)


def run_one(api: Api, path: Path, args: argparse.Namespace) -> int:
    head(f"1 · upload {path.name}  {DIM}({path.suffix[1:]}, as {args.role}){RESET}")
    doc = api.upload(path, role=args.role, synthetic=not args.real)
    ok(f"doc_id {doc['doc_id']}  sha256 {doc['sha256'][:12]}…  {doc['byte_size']} bytes")
    ok(f"read by {doc['method']}, status {doc['status']}")

    if doc["unknown_labels"]:
        info(f"{len(doc['unknown_labels'])} label(s) not recognised, left alone rather "
             f"than guessed: {list(doc['unknown_labels'])[:3]}")

    drifted = [f for f in doc["fields"] if f["status"] == "drifted"]
    for f in drifted:
        info(f"drift: {f['label']!r} was printed as {f['document_label']!r}")

    if doc["missing"]:
        print(f"  {YELLOW}{len(doc['missing'])} required field(s) not read:{RESET}")
        for label in doc["missing"]:
            print(f"      {YELLOW}{label}{RESET}")
        if not args.supply:
            raise Failed(
                "nothing is guessed, so this document cannot become a call yet. Supply the "
                "missing values:\n      "
                + " ".join(f'--supply "{m}=VALUE"' for m in doc["missing"])
            )
        values = {}
        for item in args.supply:
            label, _, value = item.partition("=")
            values[label.strip()] = value.strip()
        doc = api.supply(doc["doc_id"], values)
        ok(f"supplied by hand: {list(values)} — recorded as human_supplied in the record")
        if doc["missing"]:
            raise Failed(f"still missing: {doc['missing']}")

    if doc["validation_error"]:
        raise Failed(f"read, but did not validate: {doc['validation_error']}")

    ok(f"{len(doc['clauses'])} clauses will be spoken")
    if args.show_clauses:
        for c in doc["clauses"]:
            print(f"      {BOLD}{c['title_hi']}{RESET} {DIM}{c['clause_id']}{RESET}")
            print(f"        {c['text'][:110]}")

    head("2 · create the call")
    call = api.create_call(doc["doc_id"])
    ok(f"call {call['id']}  fallback_allowed={call['fallback_allowed']}")
    info(f"label: {call['label']}")
    info(f"kfs_ref: {call['kfs_ref']}")

    print(f"\n  {BOLD}borrower{RESET}  {BLUE}{api.base}/c/{call['id']}{RESET}")
    print(f"  {BOLD}watch  {RESET}  {BLUE}{api.base}/?call={call['id']}{RESET}")
    print(f"  {BOLD}record {RESET}  {BLUE}{api.base}/record/{call['id']}{RESET}")

    if not args.drive:
        print(f"\n{DIM}Open the borrower link to take the call for real "
              f"(needs `make agent`), or re-run with --drive to simulate it.{RESET}")
        return 0

    kind = "estimated timings" if args.fake_audio else "REAL RIME AUDIO"
    head(f"3 · take the call  {DIM}(real FSM, {kind}){RESET}")
    decision, barged = asyncio.run(drive(
        api, call, doc, barge=args.barge_in, rushed=args.rushed, pace=args.pace,
        fake_audio=args.fake_audio, do_play=args.play, save=args.save,
        realtime=args.realtime))

    expect_refused = barged or args.rushed
    if decision.granted == expect_refused:
        bad(f"expected {'REFUSED' if expect_refused else 'GRANTED'}, "
            f"got {'GRANTED' if decision.granted else 'REFUSED'}")
        return 1

    return verify(api, call, doc, granted=decision.granted, barged=barged,
                  n_clauses=len(doc["clauses"]))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Drive the whole SAMJHA flow against any KFS document.")
    ap.add_argument("--doc", type=Path, default=None,
                    help="any .pdf or .docx (default: a random fixture, either format)")
    ap.add_argument("--api", default=os.environ.get("SAMJHA_API", "http://127.0.0.1:8000"))
    ap.add_argument("--role", default="branch_helper",
                    choices=("lender_system", "branch_helper", "borrower"))
    ap.add_argument("--real", action="store_true",
                    help="declare this a real borrower's document (needs SAMJHA_ALLOW_REAL_DATA)")
    ap.add_argument("--supply", action="append", metavar='"Label=value"',
                    help="supply a field the document did not yield; repeatable")
    ap.add_argument("--drive", action="store_true",
                    help="take the call: synthesize every clause through Rime and run the FSM")
    ap.add_argument("--fake-audio", action="store_true",
                    help="skip Rime; estimate durations from text (offline/CI, not measurements)")
    ap.add_argument("--play", action="store_true",
                    help="play the audio out of the speakers, cut at the barge-in")
    ap.add_argument("--save", type=Path, default=None, metavar="DIR",
                    help="write per-clause WAVs so you can listen to them")
    ap.add_argument("--barge-in", default="auto", metavar="CLAUSE|auto|none",
                    help="which clause to interrupt mid-number (default: auto)")
    ap.add_argument("--rushed", action="store_true",
                    help="also answer the consent question too fast")
    ap.add_argument("--realtime", action="store_true",
                    help="pace each clause by its OWN audio duration, so the screen's\n"
                         "timeline matches the audio and a recording can be muxed with it")
    ap.add_argument("--pace", type=float, default=0.0, metavar="S",
                    help="seconds per clause, so a browser can be watched live")
    ap.add_argument("--show-clauses", action="store_true", help="print the Hindi to be spoken")
    ap.add_argument("--sweep", action="store_true",
                    help="every fixture in both formats; asserts pdf and docx agree")
    args = ap.parse_args()

    api = Api(args.api)
    try:
        health = api.check()
    except Failed as e:
        print(f"{RED}{e}{RESET}")
        return 2

    print(f"{BOLD}SAMJHA end-to-end{RESET}  {DIM}{api.base}{RESET}")
    info(f"livekit {'configured' if health['livekit_configured'] else 'NOT configured'}"
         f" · agent_name {health['agent_name']}"
         f" · real data {'ALLOWED' if health['allow_real_data'] else 'refused'}")
    if args.real and not health["allow_real_data"]:
        print(f"{RED}--real needs the server started with SAMJHA_ALLOW_REAL_DATA=1{RESET}")
        return 2

    # Fail here rather than three clauses into a call. `make e2e` sources .env; a bare
    # `uv run` does not, which is the likeliest way to hit this.
    if args.drive and not args.fake_audio and not os.environ.get("RIME_API_KEY"):
        print(f"{RED}RIME_API_KEY is not set, and --drive speaks through Rime.{RESET}")
        print(f"{DIM}  use `make e2e ARGS=\"--drive\"` (which sources .env), or{RESET}")
        print(f"{DIM}  set -a; . ./.env; set +a{RESET}")
        print(f"{DIM}  or pass --fake-audio to work offline.{RESET}")
        return 2

    if args.sweep:
        return sweep(api, args)

    path = args.doc or default_doc()
    if not path.exists():
        print(f"{RED}no such file: {path}{RESET}")
        return 2
    try:
        failures = run_one(api, path, args)
    except Failed as e:
        print(f"\n{RED}{e}{RESET}")
        return 1

    print()
    if failures:
        print(f"{RED}{BOLD}{failures} check(s) failed{RESET}")
        return 1
    print(f"{GREEN}{BOLD}all checks passed{RESET}")
    return 0


def sweep(api: Api, args: argparse.Namespace) -> int:
    """Every fixture, both formats, asserting the two formats agree per document."""
    head("sweep: every fixture in both formats")
    stems = sorted(p.stem for p in FIXTURES.glob("pdf/*.pdf"))
    if not stems:
        print(f"{RED}no fixtures — run `make fixtures`{RESET}")
        return 2

    failures = 0
    for stem in stems:
        row = {}
        for fmt in ("pdf", "docx"):
            path = FIXTURES / fmt / f"{stem}.{fmt}"
            if not path.exists():
                continue
            try:
                doc = api.upload(path, role=args.role, synthetic=True)
            except Failed as e:
                bad(f"{stem}.{fmt}: {e}")
                failures += 1
                continue
            row[fmt] = doc

        if not row:
            continue
        texts = {fmt: [c["text"] for c in d["clauses"]] for fmt, d in row.items()}
        agree = len(set(map(tuple, texts.values()))) == 1
        status = row.get("pdf") or row["docx"]
        n = len(status["clauses"])
        missing = status["missing"]

        if missing:
            bad(f"{stem:<34} {n:>2} clauses  MISSING {missing}")
            failures += 1
        elif not agree:
            bad(f"{stem:<34} pdf and docx disagree")
            failures += 1
        else:
            formats = "+".join(row)
            ok(f"{stem:<34} {n:>2} clauses  {formats} agree")

    print()
    if failures:
        print(f"{RED}{BOLD}{failures} document(s) failed{RESET}")
        return 1
    print(f"{GREEN}{BOLD}{len(stems)} documents, both formats, all agree{RESET}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130) from None
