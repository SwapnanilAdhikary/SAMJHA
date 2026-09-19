"""Go / no-go for a LIVE stage demo. Run it from the podium, before you start talking.

    make stage                                  # localhost only
    make stage URL=https://x.trycloudflare.com  # ...and the tunnel the phone will use

Checks the things that actually kill a live call, in the order they kill it, and prints
the one command that fixes each. Exit 0 means every stage-critical check passed.

Deliberately NOT a health endpoint: the failures that matter here are cross-process
(terminal 2 never started, the tunnel died, the key is present but revoked), and no
single server can see them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

G, Y, R, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
fails: list[str] = []
warns: list[str] = []


def ok(name: str, detail: str = "") -> None:
    print(f"  {G}PASS{X}  {name:<22} {detail}")


def warn(name: str, detail: str, fix: str) -> None:
    print(f"  {Y}WARN{X}  {name:<22} {detail}\n        {Y}->{X} {fix}")
    warns.append(name)


def fail(name: str, detail: str, fix: str) -> None:
    print(f"  {R}FAIL{X}  {name:<22} {detail}\n        {R}->{X} {fix}")
    fails.append(name)


def section(title: str) -> None:
    print(f"\n{B}{title}{X}")


# ------------------------------------------------------------------ keys
def check_modes() -> None:
    """Modes that change what the borrower actually hears. Never demo in one by accident."""
    section("0 · what this call will read")
    from kfs import build_clauses

    if build_clauses.skip_intro():
        warn("SAMJHA_SKIP_INTRO", "ON — greeting skipped, the call opens on the account "
                                  "number (~12s faster to the first question)",
             "intended for the stage. unset it for anything a real borrower hears; "
             "the agent writes a note into the call's event log either way")
    else:
        ok("SAMJHA_SKIP_INTRO", "off — full greeting, ~12s before the account number")

    # A missing prompts/ is not a 404 the borrower sees — it is the page falling back to
    # the BROWSER's own voice, which on macOS is female while Rime's taru is male. Two
    # voices, one of them talking over the disclosure.
    prompts = sorted((ROOT / "web" / "prompts").glob("*.wav"))
    if len(prompts) >= 6:
        ok("borrower prompts", f"{len(prompts)} Rime-rendered prompts — one voice on the page")
    else:
        fail("borrower prompts", f"only {len(prompts)} in web/prompts/ — the page will fall "
                                 "back to the browser's own (female) voice over Rime's",
             "make prompts   (needs RIME_API_KEY; ~36s of audio)")

    from agent.session import TURN_HANDLING
    if TURN_HANDLING["interruption"]["enabled"]:
        ok("voice barge-in", f"on, min_duration {TURN_HANDLING['interruption']['min_duration']}s "
                             "— the stop button works too")
    else:
        warn("voice barge-in", "OFF (SAMJHA_VOICE_BARGE_IN=0) — speaking will NOT interrupt",
             "interrupt with the on-screen stop button; it is the same code path")

    if os.environ.get("SAMJHA_ALLOW_REAL_DATA"):
        fail("SAMJHA_ALLOW_REAL_DATA", "ON — the server will accept non-synthetic uploads",
             "unset it before a public demo unless you meant it")
    else:
        ok("SAMJHA_ALLOW_REAL_DATA", "off — synthetic uploads only")


def check_keys() -> None:
    section("1 · credentials")
    if not (ROOT / ".env").exists():
        fail(".env", "not found", "cp .env.example .env, then paste RIME_API_KEY")
        return
    if not os.environ.get("RIME_API_KEY"):
        fail("RIME_API_KEY", "unset — nothing will speak", "add it to .env; make stage sources .env")
    else:
        ok("RIME_API_KEY", "present")

    if os.environ.get("SARVAM_API_KEY"):
        ok("SARVAM_API_KEY", "present — she can answer by voice")
    else:
        warn("SARVAM_API_KEY", "unset — teach-back cannot hear her",
             "the call still runs; make talk ARGS=--typed is the terminal fallback")

    if os.environ.get("OPENROUTER_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        ok("LLM key", "present — LLM teach-back grading")
    else:
        warn("LLM key", "unset", "falls back to the offline grader; grading is stricter, not absent")


# ------------------------------------------------------------------ rime
def check_rime() -> None:
    section("2 · Rime (the only thing that speaks)")
    if not os.environ.get("RIME_API_KEY"):
        fail("Rime synth", "skipped, no key", "see above")
        return

    import contextlib
    import io

    from delivery import config_guard
    try:
        # config_guard.main() prints its own report; we only want its verdict here.
        with contextlib.redirect_stdout(io.StringIO()):
            rc = config_guard.main()
        if rc != 0:
            fail("Rime catalog", "coda/taru/hi absent from the LIVE catalog",
                 "a voice was retired — do NOT demo until this passes")
            return
        ok("Rime catalog", f"{config_guard.MODEL_ID}/{config_guard.SPEAKER}/{config_guard.LANG}")
    except Exception as e:
        fail("Rime catalog", f"{type(e).__name__}: {e}", "network is down, or Rime is")
        return

    # A key that is merely present proves nothing — a revoked key returns 401 only on use.
    import asyncio

    from delivery import rime_ws3
    try:
        r = asyncio.run(rime_ws3.synthesize(["नमस्ते"]))
        if r.duration_s > 0.2:
            ok("Rime /ws3", f"{r.duration_s:.2f}s of real audio, mu-law 8 kHz")
        else:
            fail("Rime /ws3", f"returned only {r.duration_s:.2f}s", "the socket connected but produced no speech")
    except Exception as e:
        fail("Rime /ws3", f"{type(e).__name__}: {e}", "key revoked, out of credit, or the venue blocks wss://")


# ------------------------------------------------------------------ processes
def check_server(base: str) -> dict:
    section("3 · terminal 1 — the API and the three surfaces")
    try:
        h = httpx.get(f"{base}/health", timeout=5.0).json()
    except Exception as e:
        fail("make serve", f"{base} unreachable ({type(e).__name__})", "make serve")
        return {}
    ok("make serve", f"{base} up")

    # One stylesheet now carries the palette for all three surfaces. A 404 here does not
    # degrade them — it leaves every page unstyled, which is a worse demo than no demo.
    try:
        r = httpx.get(f"{base}/tokens.css", timeout=5.0)
        if r.status_code == 200 and "--go" in r.text:
            ok("tokens.css", "the shared palette serves — one look across all surfaces")
        else:
            fail("tokens.css", f"HTTP {r.status_code} — every page will render UNSTYLED",
                 "web/tokens.css is missing or empty; restore it before you present")
    except Exception as e:
        fail("tokens.css", f"{type(e).__name__}", "the shared palette is unreachable")

    if h.get("livekit_configured"):
        ok("LiveKit config", "URL, key and secret all set")
    else:
        fail("LiveKit config", "incomplete — /c/{id} will 503 on the token route",
             "fill LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET in .env, then restart make serve")
    return h


def check_agent(h: dict) -> None:
    section("4 · terminal 2 — the consent worker")
    # ponytail: pgrep, not a LiveKit worker query. The failure this guards is "you forgot
    # terminal 2", which pgrep sees. Swap for an agent_dispatch round-trip if a stale
    # process ever passes this check while failing to take a dispatch.
    running = subprocess.run(["pgrep", "-f", r"agent\.main"], capture_output=True).returncode == 0
    if running:
        ok("make agent", f"worker process up, agent_name={h.get('agent_name', '?')}")
    else:
        fail("make agent", "no worker process — the borrower waits on a screen that never changes",
             "make agent   (wait for 'registered worker' before you tap)")


def check_https(url: str | None) -> None:
    section("5 · HTTPS for the borrower's microphone")
    if not url:
        warn("tunnel", "not given",
             "cloudflared tunnel --url http://localhost:8000, then: make stage URL=https://...")
        return
    if not url.startswith("https://"):
        fail("tunnel", f"{url} is not https — getUserMedia denies the mic with no dialog",
             "the borrower page needs a secure context; use the tunnel URL, not localhost")
        return
    try:
        r = httpx.get(f"{url}/health", timeout=15.0, follow_redirects=True)
        r.raise_for_status()
        ok("tunnel", f"{url} serves this app")
    except Exception as e:
        fail("tunnel", f"{url} unreachable ({type(e).__name__})",
             "the tunnel died — restart cloudflared and re-run with the NEW url")


# ------------------------------------------------------------------ fallbacks
def check_fallbacks(base: str) -> None:
    section("6 · what you fall back to when the wifi dies")
    for rel, why in [
        ("demo/demo_kfs.pdf", "the document you upload on stage"),
        ("demo/evidence_ab.wav", "16s: the account number raw, then through the delivery layer"),
        ("demo/bargein_cut.wav", "20s: 96.7% of the clause, and the number still not heard"),
        ("video/samjha_demo.mp4", "179s: the whole flow, if nothing else works"),
    ]:
        p = ROOT / rel
        if p.exists():
            ok(Path(rel).name, why)
        else:
            warn(Path(rel).name, "missing", f"you lose: {why}")

    if base:
        try:
            httpx.get(f"{base}/health", timeout=5.0).raise_for_status()
            ok("DEMO MODE", "the button on /panel replays a scripted call through the real ingest path")
        except Exception:
            pass


def main() -> int:
    url = None
    for a in sys.argv[1:]:
        if a.startswith("http"):
            url = a.rstrip("/")
    base = os.environ.get("SAMJHA_BASE", "http://localhost:8000").rstrip("/")

    print(f"\n{B}SAMJHA — stage check{X}    local={base}    tunnel={url or '(none given)'}")
    check_modes()
    check_keys()
    check_rime()
    h = check_server(base)
    check_agent(h)
    check_https(url)
    check_fallbacks(base)

    print()
    if fails:
        print(f"{R}{B}NO-GO{X}  {len(fails)} blocking: {', '.join(fails)}")
        print(f"       Fix them, or open /panel and use {B}DEMO MODE{X} — real UI, real record, scripted timing.")
        return 1
    if warns:
        print(f"{Y}{B}GO, degraded{X}  {len(warns)} warning(s): {', '.join(warns)}")
        return 0
    print(f"{G}{B}GO{X}  every check passed. Open /intake and upload demo/demo_kfs.pdf.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
