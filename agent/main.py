"""Entrypoint. livekit-agents 1.8.0: AgentServer + @server.rtc_session() + cli.run_app().

    make agent                            # register the worker and wait for a dispatch
    uv run python -m agent.main dev
    uv run python -m agent.main --help
    uv run python -m agent.main selfcheck # offline: no LiveKit, no network

The clauses for a call come from THE DOCUMENT THAT CALL WAS CREATED WITH, fetched per job
from `GET /calls/{call_id}/kfs`. `ctx.room.name` is the call id, so nothing else has to be
plumbed through LiveKit.

This replaced a process-global `SAMJHA_KFS` env var, which was not a style problem: one
worker serves many rooms concurrently, and a single module-level default meant two
borrowers on two different loans would both have been read whichever fixture the process
started with — the wrong loan's numbers in someone's sha256-sealed consent record. The KFS
now lives in the job's own coroutine, and the agent re-verifies the document's sha256
against the bytes the store holds, so serving the wrong loan would require the dispatch,
the stored row and the file on disk to all agree.

`SAMJHA_KFS` survives for offline development only, and when it fires the call emits a
`note` saying the loan being read is a fixture rather than a borrower's document.

⚠️ `agent_name` makes this worker EXPLICIT-DISPATCH ONLY, and that is deliberate. A worker
registered without one is dispatched to every room created in the LiveKit project —
including rooms belonging to any other app sharing these credentials — where it would find
no call row and have nothing sensible to read.

ALL FIXTURE DATA IS SYNTHETIC. A real uploaded document is accepted only when the server
was started with SAMJHA_ALLOW_REAL_DATA set; see api/intake.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from livekit.agents import AgentServer, JobContext, cli

from agent.session import (
    ConsentAgent,
    PlayoutLedger,
    RimeSocketPool,
    build_session,
    new_fsm,
    run_consent_flow,
    wire_transcripts,
)
from api import events
from kfs.build_clauses import build_clauses
from kfs.clauses import Clause
from kfs.schema import KFS

load_dotenv()
logger = logging.getLogger("samjha.agent")

# Offline fallback only. A real call resolves its loan from the call row.
DEFAULT_KFS = "fixtures/synthetic/kfs_02_personal.json"

# Must match api/main.AGENT_NAME, or dispatches are created for a worker that never
# registered and the borrower waits on a screen that never changes.
AGENT_NAME = "samjha-consent"

API_BASE = os.environ.get("SAMJHA_API", "http://127.0.0.1:8000")

server = AgentServer()


def load_clauses(kfs: KFS) -> list[Clause]:
    """Build the clause list for one loan.

    Pure, and called once per job rather than cached: `agent/session.py:320` assigns
    `seg.audio` in place, so a shared list would carry one borrower's synthesized audio
    into the next call.

    The identifier clause reads first, which is not cosmetic: `account_identifier` is the
    category the day-1 pilot found Rime reads as an Indian-scale quantity rather than a
    digit sequence (raw 1/5 vs delivery-layer 5/5). It is the claim the demo has to show.
    """
    return build_clauses(kfs)


def fetch_kfs(call_id: str, *, base: str | None = None,
              timeout: float = 10.0) -> tuple[KFS, dict]:
    """(KFS, provenance) for this call, from the API. Raises if the call has no document.

    The sha256 in the response is checked against the stored source bytes when they are
    reachable on this machine, so a record's provenance block cannot name a document whose
    content changed underneath it.
    """
    url = f"{(base or API_BASE).rstrip('/')}/calls/{call_id}/kfs"
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    body = r.json()

    kfs = KFS.model_validate(body["kfs"])
    provenance = body.get("provenance") or {}
    _verify_source(body.get("sha256", ""), provenance)
    return kfs, provenance


def _verify_source(sha256: str, provenance: dict) -> None:
    """Re-hash the stored source document, when this process can see it.

    Best-effort by design: the agent may not share a filesystem with the API. A MISMATCH
    is fatal — that means the bytes the record cites are not the bytes that were read — but
    an absent file is only logged, because "not co-located" is not "tampered with".
    """
    if not sha256:
        return
    suffix = {"pdf_table": ".pdf", "docx_table": ".docx"}.get(provenance.get("method", ""), "")
    if not suffix:
        return
    path = Path(os.environ.get("SAMJHA_KFS_DIR", "data/kfs")) / f"{sha256}{suffix}"
    if not path.exists():
        logger.info("source document not local, skipping hash re-verification: %s", path)
        return
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != sha256:
        raise ValueError(
            f"source document hash mismatch for {path}: record cites {sha256}, "
            f"file is {actual}. Refusing to read a loan whose source changed."
        )


def _fixture_clauses(path: str | None = None) -> tuple[list[Clause], str]:
    src = Path(path or os.environ.get("SAMJHA_KFS", DEFAULT_KFS))
    kfs = KFS.model_validate_json(src.read_text(encoding="utf-8"))
    return load_clauses(kfs), str(src)


class KFSUnavailable(RuntimeError):
    """The call may have a document, but we could not read it. Refuse rather than guess."""


def clauses_for_call(call_id: str) -> tuple[list[Clause], dict, str]:
    """(clauses, provenance, source note).

    Falls back to a fixture ONLY when the API positively says this call has no document —
    the offline-development path, where the room is a bare id and there is nothing else to
    read. Any other failure raises `KFSUnavailable`.

    That distinction is load-bearing. `api/records.py` stamps provenance from the CALL ROW,
    so if the agent read a fixture for a call that does have a document attached, the
    sealed record would cite the uploaded document's filename and sha256 while the clauses
    it contains came from a different loan entirely. That is the exact failure
    `store.allow_fallback` was written to prevent on the API side, and a transport error
    must not reintroduce it here. A borrower hearing nothing is recoverable; a borrower
    consenting to someone else's numbers is not.
    """
    try:
        kfs, provenance = fetch_kfs(call_id)
        return load_clauses(kfs), provenance, ""
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise KFSUnavailable(
                f"{API_BASE} answered {e.response.status_code} for call {call_id}"
            ) from e
        # A clean 404 means the API is up and says: no confirmed document for this call.
    except httpx.RequestError as e:
        # Connection refused, DNS, timeout — we do not know what this call is about.
        raise KFSUnavailable(
            f"cannot reach the API at {API_BASE} ({type(e).__name__}). Is it running, and "
            f"is SAMJHA_API pointing at it?"
        ) from e
    except (ValueError, KeyError, TypeError) as e:
        # Malformed body, or the source-document hash did not match what the record cites.
        raise KFSUnavailable(f"the KFS for {call_id} did not load: {e}") from e

    clauses, src = _fixture_clauses()
    note = (
        f"This call has no attached document, so the clauses below come from the SYNTHETIC "
        f"FIXTURE {src} and describe a specimen loan, not a borrower's document."
    )
    logger.warning("no document for %s; reading fixture %s", call_id, src)
    return clauses, {}, note


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    call_id = ctx.room.name or "unknown-room"

    try:
        clauses, provenance, note = clauses_for_call(call_id)
    except KFSUnavailable as e:
        # Say so, write it down, and hang up. Reading ANY other loan here would seal a
        # record whose provenance names the borrower's document and whose clauses do not
        # come from it.
        logger.error("refusing call %s: %s", call_id, e)
        events.emit(
            call_id, "note",
            text=f"REFUSED TO READ: {e} Nothing was read to the borrower, and no consent "
                 f"was taken. This is not a failed call — it is a call that correctly did "
                 f"not happen.",
        )
        events.emit(call_id, "consent", decision="REFUSED",
                    reason=f"kfs_unavailable: {e}", flagged_for_callback=True)
        events.emit(call_id, "call_end")
        session = build_session()
        await session.start(ConsentAgent(), room=ctx.room)
        await session.say(
            "क्षमा कीजिए, अभी आपके दस्तावेज़ की जानकारी नहीं मिल पाई। "
            "हमारा प्रतिनिधि आपको फिर कॉल करेगा।"
        ).wait_for_playout()
        return

    fsm = new_fsm(clauses, call_id)

    if note:
        events.emit(call_id, "note", text=note)
    elif provenance:
        events.emit(
            call_id, "note",
            text=f"Reading {provenance.get('filename') or provenance.get('doc_id')} "
                 f"(sha256 {str(provenance.get('sha256', ''))[:12]}…, "
                 f"read by {provenance.get('method')}"
                 + ("" if provenance.get("synthetic", True) else ", REAL BORROWER DATA")
                 + ").",
        )

    # Register the clauses with their Hindi titles, durations and key values BEFORE
    # delivery. Without this the panel has only what agent/consent_fsm.py writes, which
    # carries no `title_hi` at all (api/events.py:211), so the whole KFS renders as bare
    # clause ids — "identity", "apr", "grievance". `clause_payload` already builds exactly
    # the right shape and, until now, nothing called it.
    for ordinal, clause in enumerate(clauses):
        events.emit(call_id, "clause_registered", ordinal=ordinal,
                    **events.clause_payload(clause))

    session = build_session()
    answers = wire_transcripts(session)
    ledger = PlayoutLedger()

    pool = RimeSocketPool()
    await pool.start()  # opens the live socket AND the warm spare before anyone speaks

    try:
        await session.start(ConsentAgent(), room=ctx.room)
        ledger.attach(session.output.audio)

        decision = await run_consent_flow(session, fsm, clauses, pool, ledger, answers)

        if decision is None:
            logger.info("call ended with no consent utterance", extra={"call_id": call_id})
        elif decision.granted:
            await session.say("धन्यवाद। आपकी सहमति दर्ज कर ली गई है।").wait_for_playout()
        else:
            # The refusal is already in events/{call_id}.jsonl. Say so out loud too: a
            # borrower who is refused deserves to hear why, not just be hung up on.
            await session.say(
                "अभी सहमति दर्ज नहीं की जा सकती। हमारा प्रतिनिधि आपको फिर से कॉल करेगा।"
            ).wait_for_playout()
            logger.info("consent refused: %s", decision.reasons, extra={"call_id": call_id})
    finally:
        await pool.aclose()
        events.emit(call_id, "call_end")


def _demo() -> None:
    """Offline self-check: no LiveKit, no network, no credentials.

    Covers the two things that would silently serve the wrong loan — clause building being
    per-call, and the registration events carrying Hindi titles.
    """
    import tempfile

    # 1. Clauses are built per call, not shared. session.deliver_clause assigns seg.audio
    #    in place, so two calls sharing one Clause graph would leak audio between
    #    borrowers.
    a, src = _fixture_clauses()
    b, _ = _fixture_clauses()
    assert [c.id for c in a] == [c.id for c in b]
    assert a[0] is not b[0], "clause objects must not be shared between calls"
    a[0].segments[0].audio = b"\x00" * 8000
    assert b[0].segments[0].audio == b"", "mutating one call's audio touched another's"

    # 2. Two different fixtures give different loans — the concurrency bug SAMJHA_KFS
    #    could not avoid.
    root = Path("fixtures/synthetic")
    one, _ = _fixture_clauses(root / "kfs_01_two_wheeler.json")
    two, _ = _fixture_clauses(root / "kfs_02_personal.json")

    def acct(clauses: list[Clause]) -> str:
        return next(kv.raw_text for c in clauses for kv in c.key_values
                    if kv.kind == "account_identifier")

    assert acct(one) != acct(two)

    # 3. Registration events carry title_hi, so the panel shows Hindi rather than ids.
    #    A FRESH list: check 1 deliberately mutated a[0]'s audio.
    fresh, _ = _fixture_clauses()
    with tempfile.TemporaryDirectory() as td:
        os.environ["SAMJHA_EVENTS_DIR"] = td
        for ordinal, clause in enumerate(fresh):
            events.emit("selfcheck", "clause_registered", ordinal=ordinal,
                        **events.clause_payload(clause))

        lines = [json.loads(x) for x in
                 (Path(td) / "selfcheck.jsonl").read_text("utf-8").splitlines()]
        assert len(lines) == len(fresh)
        assert all(x["title_hi"] for x in lines), "a clause registered with no Hindi title"
        # Nothing synthesized yet, so every duration is 0 and no key value can read as
        # heard. The panel must show the whole KFS greyed out before delivery begins.
        assert all(x["total_duration_s"] == 0 for x in lines)
        assert not any(kv["heard"] for x in lines for kv in x["key_values"])
        titled = {x["clause_id"]: x["title_hi"] for x in lines}
        assert titled["apr"] == "सालाना कुल दर"
        assert titled["identity"] == "लोन की पहचान"
        del os.environ["SAMJHA_EVENTS_DIR"]

    # 4. A hash mismatch on the source document is fatal; an absent file is not.
    _verify_source("a" * 64, {"method": "pdf_table"})  # not on disk: logged, not raised
    with tempfile.TemporaryDirectory() as td:
        os.environ["SAMJHA_KFS_DIR"] = td
        digest = hashlib.sha256(b"real bytes").hexdigest()
        (Path(td) / f"{digest}.pdf").write_bytes(b"tampered")
        try:
            _verify_source(digest, {"method": "pdf_table"})
        except ValueError as e:
            assert "hash mismatch" in str(e)
        else:
            raise AssertionError("a changed source document must be refused")
        del os.environ["SAMJHA_KFS_DIR"]

    # 5. THE DISTINCTION THAT MATTERS. An unreachable API must NOT quietly become "read
    #    the fixture": api/records.py stamps provenance from the call row, so a fixture
    #    read for a document-bound call would seal a record citing the borrower's
    #    document alongside another loan's numbers.
    import httpx as _httpx

    real_base = globals()["API_BASE"]
    try:
        globals()["API_BASE"] = "http://127.0.0.1:1"  # nothing listens here
        try:
            clauses_for_call("some-call")
        except KFSUnavailable as e:
            assert "cannot reach the API" in str(e)
        else:
            raise AssertionError("an unreachable API must refuse, never read a fixture")

        # A clean 404 is different: the API is up and says this call has no document.
        # That is the offline-dev path, and it discloses itself.
        def _404(url, **kw):
            request = _httpx.Request("GET", url)
            response = _httpx.Response(404, request=request, json={"detail": "no document"})
            raise _httpx.HTTPStatusError("404", request=request, response=response)

        real_get = _httpx.get
        try:
            _httpx.get = _404
            _, prov, note = clauses_for_call("bare-room")
            assert prov == {} and "SYNTHETIC FIXTURE" in note
        finally:
            _httpx.get = real_get
    finally:
        globals()["API_BASE"] = real_base

    print(f"agent OK — {len(a)} clauses per call from {src}, titles registered, "
          f"source hash verified, fixture fallback disclosed")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "selfcheck":
        _demo()
    else:
        cli.run_app(server)
