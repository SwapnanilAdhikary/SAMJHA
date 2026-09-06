"""Be the borrower. A real call, on your machine, with no LiveKit and no fixtures.

Everything here is the real thing: Rime Coda synthesises the clauses, the audio goes through
the same 8 kHz telephone channel the eval measures, your voice goes to Sarvam, teach-back is
graded against the extracted facts, and the consent FSM decides. The only part that is not
the shipping product is the transport — this plays out of your speakers instead of a room.

    uv run python scripts/talk.py                # speak your answers
    uv run python scripts/talk.py --typed        # type them instead (no mic needed)
    uv run python scripts/talk.py --doc 3        # a different loan

Controls, and there are only two:
    ENTER while it is speaking   -> interrupt (this is the barge-in the product is about)
    ENTER while it is listening  -> done answering
"""

from __future__ import annotations

import argparse
import asyncio
import json
import select
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import teachback  # noqa: E402
from agent.consent_fsm import ConsentFSM  # noqa: E402
from agent.rushed_consent import evaluate  # noqa: E402
from agent.session import ulaw_to_pcm16  # noqa: E402
from delivery import rime_ws3  # noqa: E402
from kfs.build_clauses import build_clauses  # noqa: E402
from kfs.schema import KFS  # noqa: E402

RATE = 8000
DIM, BOLD, GREEN, RED, YELLOW, RESET = "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"

STATE_COLOUR = {
    "UNDERSTOOD": GREEN, "HEARD": YELLOW, "PARTIALLY_HEARD": RED,
    "TEACH_BACK_FAIL": RED, "UNHEARD": DIM,
}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def enter_pressed() -> bool:
    """True if a line is waiting on stdin. Non-blocking, so playback is not held up."""
    if select.select([sys.stdin], [], [], 0)[0]:
        sys.stdin.readline()
        return True
    return False


def drain_stdin() -> None:
    """Discard buffered input before playback starts.

    Observed in a real run: the ENTER that ends teach-back recording was still sitting in
    the buffer when the next clause began, so the clause was 'interrupted' at 0.1s and
    marked PARTIALLY_HEARD without the listener touching anything. A barge-in the user did
    not perform is worse than a missed one — it puts a false event in the consent record.
    """
    while select.select([sys.stdin], [], [], 0)[0]:
        if not sys.stdin.readline():
            break


def play_interruptible(ulaw: bytes) -> tuple[float, bool]:
    """Play mu-law audio. Return (seconds ACTUALLY played, interrupted).

    Frames are counted in the stream callback rather than timed with a wall clock, because
    the whole product turns on how much audio really reached the listener. This is the
    local stand-in for LiveKit's PlaybackFinishedEvent.playback_position.
    """
    pcm = np.frombuffer(ulaw_to_pcm16(ulaw), dtype="<i2").astype(np.float32) / 32768.0
    played = 0
    done = threading.Event()

    def cb(outdata, frames, _t, _status):
        nonlocal played
        chunk = pcm[played:played + frames]
        outdata[: len(chunk), 0] = chunk
        outdata[len(chunk):, 0] = 0.0
        played += len(chunk)
        if played >= len(pcm):
            raise sd.CallbackStop

    interrupted = False
    with sd.OutputStream(samplerate=RATE, channels=1, dtype="float32",
                         callback=cb, finished_callback=done.set):
        while not done.wait(0.05):
            if enter_pressed():
                interrupted = True
                break

    return played / RATE, interrupted


def listen(max_s: float = 20.0) -> bytes:
    """Record until ENTER (or max_s). Returns a WAV in memory for Sarvam."""
    import io
    import wave

    buf: list[np.ndarray] = []
    with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                        callback=lambda ind, *_: buf.append(ind.copy())):
        start = time.monotonic()
        while time.monotonic() - start < max_s:
            if enter_pressed():
                break
            time.sleep(0.05)

    audio = np.concatenate(buf) if buf else np.zeros((1, 1), dtype="int16")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(audio.tobytes())
    return out.getvalue()


def transcribe(wav: bytes) -> str:
    """Your voice -> text, via Sarvam. Falls back to typing if it fails."""
    import os
    import tempfile

    from scripts.smoke import sarvam_transcribe

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav)
        p = Path(f.name)
    try:
        return sarvam_transcribe(p)
    finally:
        p.unlink(missing_ok=True)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", type=int, default=1, help="which KFS fixture (1-10)")
    ap.add_argument("--typed", action="store_true", help="type answers instead of speaking")
    ap.add_argument("--clauses", type=int, default=4, help="how many clauses to read")
    args = ap.parse_args()

    docs = sorted(Path("fixtures/synthetic").glob("kfs_*.json"))
    kfs = KFS.model_validate_json(docs[args.doc - 1].read_text())
    clauses = build_clauses(kfs)[: args.clauses]

    say(f"\n{BOLD}SAMJHA — आपका ऋण{RESET}  {DIM}({docs[args.doc-1].name}, synthetic){RESET}")
    say(f"{DIM}Rime coda/taru/hi · 8 kHz mu-law · {len(clauses)} clauses{RESET}")
    say(f"{DIM}ENTER while it speaks = interrupt.  ENTER while it listens = done.{RESET}\n")
    input(f"{BOLD}Press ENTER to take the call...{RESET}")

    fsm = ConsentFSM(clauses, call_id=f"talk-{int(time.time())}")
    first_clause_started = time.monotonic()

    for c in clauses:
        say(f"\n{BOLD}── {c.title_hi}{RESET}")
        text = " ".join(s.text for s in c.segments)
        say(f"{DIM}{text}{RESET}")

        # One flush segment per chunk: the same delivery the eval measures.
        try:
            res = await rime_ws3.synthesize(
                [s.text for s in c.segments],
                key_value_flags=[s.carries_key_value for s in c.segments])
        except Exception as e:
            # Rime is US-only and the connect occasionally times out from India. A clause we
            # could not speak was certainly not heard, so record that and move on rather
            # than ending the call — the FSM already refuses consent on a clause in this
            # state, which is the correct outcome.
            say(f"  {RED}could not synthesise this clause: {type(e).__name__}{RESET}")
            say(f"  {DIM}recording it as undelivered; consent stays blocked on it{RESET}")
            fsm.begin_delivery(c.id)
            fsm.delivery_dropped(c.id, note=f"tts_failed:{type(e).__name__}")
            continue
        for seg, out in zip(c.segments, res.segments):
            seg.audio = bytes(out.audio)

        fsm.begin_delivery(c.id)
        say(f"{DIM}speaking… (ENTER to interrupt){RESET}")
        drain_stdin()
        played, interrupted = play_interruptible(res.audio)
        state = fsm.end_delivery(c.id, played_s=played, interrupted=interrupted)

        pct = 100.0 * played / max(c.total_duration_s, 1e-9)
        col = STATE_COLOUR.get(str(state.value if hasattr(state, "value") else state), "")
        say(f"  heard {played:.1f}s of {c.total_duration_s:.1f}s ({pct:.0f}%)  ->  "
            f"{col}{state}{RESET}")

        heard = c.heard_key_values(played)
        for kv in c.key_values:
            mark = f"{GREEN}heard{RESET}" if kv in heard else f"{RED}NOT heard{RESET}"
            say(f"     {kv.kind:<20} {kv.value}   {mark}")

        if str(state).endswith("PARTIALLY_HEARD"):
            say(f"  {RED}Consent is blocked on this clause. It must be re-read from the "
                f"start.{RESET}")
            continue

        # Teach-back: say it back in your own words.
        say(f"\n  {BOLD}अपने शब्दों में बताइए — इस बात का क्या मतलब है?{RESET}")
        if args.typed:
            transcript = input("  > ").strip()
        else:
            say(f"  {DIM}listening… (ENTER when done){RESET}")
            wav = listen()
            try:
                transcript = transcribe(wav)
            except Exception as e:
                say(f"  {RED}STT failed ({type(e).__name__}). Type it instead.{RESET}")
                transcript = input("  > ").strip()
            say(f"  {DIM}heard you say: {transcript!r}{RESET}")

        result = teachback.grade(c, transcript)
        grades = result.as_dicts()
        fsm.teach_back(c.id, transcript=transcript, passed=result.passed, grades=grades)
        col = GREEN if result.passed else RED
        say(f"  teach-back: {col}{'PASS' if result.passed else 'FAIL'}{RESET}"
            + (f"  {DIM}{result.note}{RESET}" if result.note else ""))
        for g in grades:
            say(f"     {str(g.get('fact_id', '?')):<22} {g.get('verdict', '?')}")
        say(f"  -> {STATE_COLOUR.get(str(fsm.state(c.id)), '')}{fsm.state(c.id)}{RESET}")

    # Consent.
    say(f"\n{BOLD}── सहमति{RESET}")
    say("  क्या आप इन शर्तों पर सहमत हैं? (हाँ / नहीं)")
    answer = input("  > ").strip() if args.typed else None
    if answer is None:
        say(f"  {DIM}listening… (ENTER when done){RESET}")
        try:
            answer = transcribe(listen(10))
        except Exception:
            answer = input("  > ").strip()
    say(f"  {DIM}you said: {answer!r}{RESET}")

    decision = evaluate(fsm, answer,
                        latency_s=time.monotonic() - first_clause_started)
    say("")
    if decision.granted:
        say(f"  {GREEN}{BOLD}CONSENT RECORDED{RESET}")
    else:
        say(f"  {RED}{BOLD}CONSENT REFUSED{RESET}")
        for r in decision.reasons:
            say(f"     {RED}{r}{RESET}")
        say(f"  {DIM}Flagged for human callback. The refusal is in the record.{RESET}")

    say(f"\n{BOLD}Final clause states{RESET}")
    for cid, st in fsm.states().items():
        say(f"  {STATE_COLOUR.get(str(st), '')}{str(st):<18}{RESET} {cid}")
    say(f"\n{DIM}Full transition log: {len(fsm.log)} events{RESET}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        say("\ncall ended")
