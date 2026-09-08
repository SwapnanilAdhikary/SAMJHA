"""Record a demo video of the whole flow, by driving the real UI in a real browser.

    make serve                                  # in another terminal
    uv run python scripts/record_demo.py        # -> video/samjha_demo.mp4

Playwright drives Chromium through the three surfaces in order — the officer's intake
desk, then the borrower's page, then the judge's evidence panel — while `scripts/e2e.py`
runs the call underneath: real Rime synthesis, the real consent FSM, real events into
`events/{call_id}.jsonl`, which is what the pages are reacting to on screen.

`--realtime` paces each clause by its OWN measured audio duration, so the on-screen
heard-through bar advances at exactly the rate the audio plays. That is what makes the
captured Rime track muxable with the silent screen capture.

TWO HONEST LIMITS, both stated in demo/README.md as well:

  * **Browser video recording has no audio.** Chromium records the page, not the speakers.
    So the audio track is the same run's Rime output, captured to WAV by e2e and muxed in
    afterwards. It is the same synthesis, at the same durations, for the same document —
    not a re-recording, and not a different take. But it is muxed, not captured.
  * **No agent process speaks over WebRTC in this recording.** The borrower page really
    connects to a real LiveKit room with a real token, but the clause states come from the
    event stream that e2e is writing. Running `make agent` gives a fully live call; it
    just cannot be captured with audio from here.

Everything on screen is the real product: the real pages, the real parse of a real
document, the real state machine, the real refusal.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

# Imported here, on the MAIN THREAD, purely for its side effects. e2e writes its WAVs with
# `agent.session.ulaw_to_pcm16`, and importing `agent.session` pulls in
# `livekit.plugins.rime`, which raises "Plugins must be registered on the main thread" if
# that import first happens inside the worker thread below.
from agent.session import ulaw_to_pcm16  # noqa: E402,F401
from scripts.e2e import Api, Failed, deliver_call, prepare  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
VIDEO = ROOT / "video"

W, H = 1280, 800

# Fake media so getUserMedia resolves without a prompt, and no autoplay gate. There is no
# microphone here and nothing to capture from the speakers; the audio is muxed afterwards.
CHROME_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-features=IsolateOrigins,site-per-process",
]

CLAUSE_ORDER = ("identity", "sanctioned", "tenure", "emi", "interest_rate", "rate_reset",
                "fees", "apr", "total_repayment", "prepayment", "grievance")


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def concat_audio(audio_dir: Path, out: Path, *, lead_in: float) -> float:
    """Join the per-clause WAVs in READ order, after `lead_in` seconds of silence.

    The silence is how the audio lines up: it covers the intake beats, which happen before
    a single word is spoken.
    """
    parts = [audio_dir / f"{c}.wav" for c in CLAUSE_ORDER]
    parts = [p for p in parts if p.exists()]
    if not parts:
        raise Failed(f"no clause WAVs in {audio_dir}")

    silence = out.parent / "_lead_in.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", f"{lead_in:.3f}",
         "-i", "anullsrc=r=8000:cl=mono", "-c:a", "pcm_s16le", str(silence)],
        check=True)

    listing = out.parent / "_concat.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in [silence, *parts]))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(out)], check=True)
    listing.unlink(missing_ok=True)
    silence.unlink(missing_ok=True)
    return duration(out)


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def mux(video: Path, audio: Path, out: Path) -> None:
    """Video + audio -> one mp4. Video is re-encoded; webm/vp8 is not an mp4 codec.

    The audio is PADDED with silence to the video's length rather than the pair being
    truncated with a bare `-shortest`. The call is shorter than the recording — the intake
    beats come before anyone speaks and the panel and record beats come after — so
    truncating cut the last 80 seconds, losing the two beats that show the evidence.
    """
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio),
         "-filter_complex", "[1:a]apad[a]",
         "-map", "0:v", "-map", "[a]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k", "-shortest", str(out)],
        check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Record a demo video of the whole flow.")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--doc", type=Path, default=DEMO / "demo_kfs.pdf")
    ap.add_argument("--out", type=Path, default=VIDEO / "samjha_demo.mp4")
    ap.add_argument("--headed", action="store_true", help="watch it drive")
    ap.add_argument("--fake-audio", action="store_true",
                    help="skip Rime (offline); the video will have no audio track")
    args = ap.parse_args()

    api = Api(args.api)
    try:
        api.check()
    except Failed as e:
        print(f"{e}")
        return 2
    if not args.doc.exists():
        print(f"no such document: {args.doc}")
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    video_dir = args.out.parent / "_raw"
    for stale in video_dir.glob("*.webm"):
        stale.unlink()
    audio_dir = DEMO / "audio"

    # ---- upload and create the call up front, so the page has something to show.
    log(f"uploading {args.doc.name}")
    doc = api.upload(args.doc, role="branch_helper", synthetic=True)
    if doc["missing"]:
        print(f"document is incomplete: {doc['missing']}")
        return 1
    call = api.create_call(doc["doc_id"])
    log(f"call {call['id']} · {len(doc['clauses'])} clauses")

    # ---- Synthesize BEFORE the browser opens.
    #
    # Ten clauses take about a minute through Rime, and no events are written during it.
    # Done after the borrower page is up, that silence trips the page's own no-agent
    # timeout and stamps "no agent joined this call" across the whole recording — which
    # is the warning working correctly, just aimed at the wrong thing.
    log("synthesizing the call through Rime (before recording starts)")
    clauses, provenance = asyncio.run(
        prepare(api, call, fake_audio=args.fake_audio, save=audio_dir))

    result: dict = {}

    def run_call() -> None:
        """Delivery only, in a thread, so the browser keeps rendering as it plays."""
        try:
            decision, barged = deliver_call(
                call, doc, clauses, provenance, barge="auto", rushed=False,
                pace=0.0, do_play=False, realtime=True)
            result["decision"] = decision
            result["barged"] = barged
        except Exception as e:  # noqa: BLE001
            result["error"] = e

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, args=CHROME_ARGS)
        ctx = browser.new_context(
            viewport={"width": W, "height": H},
            record_video_dir=str(video_dir),
            record_video_size={"width": W, "height": H},
            permissions=["microphone"],
            base_url=args.api,
        )
        page = ctx.new_page()

        # Stub the agent dispatch, so exactly ONE thing drives this call.
        #
        # Left alone, the borrower page dispatches the real worker, which fetches the KFS
        # from SAMJHA_API, and if that does not resolve to this server it correctly
        # REFUSES to read (agent.main.KFSUnavailable) — writing a refusal and a call_end
        # within seconds. Two writers on one call id, and the page jumps to the outcome
        # screen before a clause has played. Fulfilled rather than aborted, so the page's
        # no-agent timeout is never armed.
        page.route("**/dispatch", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"call_id":"recording","agent_name":"recording","dispatch_id":"rec"}'))

        started = time.monotonic()

        # ---------------------------------------------------- 1 · the officer's desk
        log("intake: what the document says")
        page.goto(f"{args.api}/intake")
        page.wait_for_timeout(1200)
        page.set_input_files("#file", str(args.doc))
        page.wait_for_timeout(600)
        page.click("#btn-upload")
        page.wait_for_selector("#result table", timeout=30_000)
        page.wait_for_timeout(1800)

        # Scroll the parsed fields, then the Hindi that will actually be spoken.
        page.mouse.wheel(0, 700)
        page.wait_for_timeout(1600)
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(2200)
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(2200)

        # ------------------------------------------------------- 2 · the borrower
        log("borrower: taking the call")
        page.goto(f"{args.api}/c/{call['id']}")
        page.wait_for_selector("#btn-take", timeout=15_000)
        page.wait_for_timeout(2200)          # the loan, in big digits

        lead_in = time.monotonic() - started
        threading.Thread(target=run_call, daemon=True).start()
        page.click("#btn-take")              # the one gesture
        page.wait_for_timeout(1500)

        # Let the call play out on screen at the audio's own pace.
        deadline = time.monotonic() + 260
        while time.monotonic() < deadline:
            if result:
                break
            page.wait_for_timeout(500)
        page.wait_for_timeout(2500)

        # --------------------------------------------------------- 3 · the record
        log("panel and record")
        page.goto(f"{args.api}/?call={call['id']}")
        page.wait_for_timeout(3500)
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(2500)
        page.goto(f"{args.api}/record/{call['id']}")
        page.wait_for_timeout(3000)
        page.mouse.wheel(0, 500)
        page.wait_for_timeout(2500)

        video_path = Path(page.video.path()) if page.video else None
        ctx.close()          # the webm is finalised here
        browser.close()

    if "error" in result:
        print(f"the call failed: {type(result['error']).__name__}: {result['error']}")
        return 1
    if video_path is None or not video_path.exists():
        print("playwright wrote no video")
        return 1

    decision = result.get("decision")
    log(f"consent {'GRANTED' if decision and decision.granted else 'REFUSED'}"
        f"  ·  video {duration(video_path):.1f}s")

    if args.fake_audio:
        final = args.out.with_suffix(".silent.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video_path),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                        "-pix_fmt", "yuv420p", str(final)], check=True)
        log(f"wrote {final}  (no audio: --fake-audio)")
        return 0

    audio = args.out.parent / "_track.wav"
    total = concat_audio(audio_dir, audio, lead_in=lead_in)
    log(f"audio track {total:.1f}s (lead-in {lead_in:.1f}s of silence over the intake)")
    mux(video_path, audio, args.out)
    audio.unlink(missing_ok=True)

    log(f"wrote {args.out}  ({duration(args.out):.1f}s)")
    print(f"\n  open it:  open {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
