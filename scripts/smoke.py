"""Prove every credential actually works, then point the ASR at the gate clips.

A key that is merely *present* proves nothing. Each check below makes a real call and
reports the actual error on failure, because the failure modes here are silent ones:
Rime serves the wrong model on a typo'd modelId, Deepgram misdetects Hindi as Spanish on
lang=multi, and Sarvam's model enum changed under everyone in June 2026.

    uv run python scripts/smoke.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BATTERY = Path("evals/results/battery")
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Indic multipliers, needed because ASR returns "एक लाख" not "100000".
MULTIPLIERS = {"हज़ार": 10**3, "हजार": 10**3, "लाख": 10**5, "करोड़": 10**7, "करोड": 10**7}
UNITS = {
    "शून्य": 0, "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पाँच": 5, "पांच": 5, "छह": 6,
    "छः": 6, "सात": 7, "आठ": 8, "नौ": 9, "दस": 10,
}

OK, FAIL = "  PASS", "  FAIL"
results: dict[str, bool] = {}


def report(name: str, ok: bool, detail: str = "") -> None:
    results[name] = ok
    print(f"{OK if ok else FAIL}  {name:<12} {detail}")


def digits_only(s: str) -> str:
    return re.sub(r"\D", "", s.translate(DEVANAGARI_DIGITS))


# ---------------------------------------------------------------- Rime
def check_rime() -> None:
    import asyncio

    from delivery import rime_ws3

    try:
        r = asyncio.run(rime_ws3.synthesize(["नमस्ते"]))
        ok = r.duration_s > 0.2
        report("Rime", ok, f"{r.duration_s:.2f}s audio via /ws3, coda/taru/hi")
    except Exception as e:
        report("Rime", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- Sarvam
def sarvam_transcribe(wav: Path, mode: str = "transcribe") -> str:
    """Sarvam Saaras STT. Returns the transcript, or raises with the raw body.

    NOTE: saarika:v2/v2.5 are GONE — the enum is saaras:v3 / saaras:v4 since June 2026.
    """
    last = ""
    for model in ("saaras:v4", "saaras:v3"):
        data = {"model": model, "language_code": "hi-IN", "mode": mode}
        r = httpx.post(
            "https://api.sarvam.ai/speech-to-text",
            headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
            files={"file": (wav.name, wav.read_bytes(), "audio/wav")},
            data=data,
            timeout=90.0,
        )
        if r.status_code == 200:
            return r.json().get("transcript", "")
        last = f"{model} -> HTTP {r.status_code}: {r.text[:200]}"
    raise RuntimeError(last)


def check_sarvam() -> None:
    try:
        t = sarvam_transcribe(BATTERY / "1a_devanagari.wav")
        report("Sarvam", bool(t.strip()), f"{t[:60]!r}")
    except Exception as e:
        report("Sarvam", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- Deepgram
def deepgram_transcribe(wav: Path) -> str:
    r = httpx.post(
        "https://api.deepgram.com/v1/listen",
        params={"model": "nova-3", "language": "hi", "smart_format": "true"},
        headers={
            "Authorization": f"Token {os.environ['DEEPGRAM_API_KEY']}",
            "Content-Type": "audio/wav",
        },
        content=wav.read_bytes(),
        timeout=90.0,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    alts = r.json()["results"]["channels"][0]["alternatives"]
    return alts[0]["transcript"] if alts else ""


def check_deepgram() -> None:
    try:
        t = deepgram_transcribe(BATTERY / "1a_devanagari.wav")
        report("Deepgram", bool(t.strip()), f"{t[:60]!r}")
    except Exception as e:
        report("Deepgram", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- LiveKit
def check_livekit() -> None:
    """Server API call with key+secret. wss:// must become https:// for the REST side."""
    import asyncio

    from livekit import api

    async def go() -> int:
        url = os.environ["LIVEKIT_URL"].replace("wss://", "https://").replace("ws://", "http://")
        lk = api.LiveKitAPI(url, os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        try:
            rooms = await lk.room.list_rooms(api.ListRoomsRequest())
            return len(rooms.rooms)
        finally:
            await lk.aclose()

    try:
        n = asyncio.run(go())
        report("LiveKit", True, f"authenticated, {n} room(s) active")
    except Exception as e:
        report("LiveKit", False, f"{type(e).__name__}: {str(e)[:160]}")


# ---------------------------------------------------------------- OpenRouter
def check_openrouter() -> None:
    try:
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            json={
                "model": "google/gemini-2.5-flash-lite",
                "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
                "max_tokens": 10,
            },
            timeout=60.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        msg = r.json()["choices"][0]["message"]["content"].strip()
        report("OpenRouter", True, f"{msg[:40]!r}")
    except Exception as e:
        report("OpenRouter", False, f"{type(e).__name__}: {str(e)[:160]}")


# ---------------------------------------------------------------- The gate
def check_gate() -> None:
    """Machine proxy for tonight's gate: does 104596 survive each rendering?

    This does NOT replace listening — ASR error and human misunderstanding are different
    things, and that distinction is pre-registered in ACCEPTANCE.md. But if the raw arm
    loses the value and the normalised arm keeps it, that is the claim showing up in data.
    """
    target = "104596"
    print("\nGATE — is the value recovered? (target 104596)\n")
    print(f"  {'clip':<18}{'scorer':<10}{'digits found':<22}{'exact'}")
    print("  " + "-" * 58)

    for clip in ["3a_raw_grouped", "3b_raw_plain", "3c_normalized"]:
        wav = BATTERY / f"{clip}.wav"
        for scorer, fn in [("sarvam", sarvam_transcribe), ("deepgram", deepgram_transcribe)]:
            try:
                t = fn(wav)
                found = digits_only(t)
                hit = target in found
                shown = (found or "—")[:20]
                print(f"  {clip:<18}{scorer:<10}{shown:<22}{'YES' if hit else 'no'}")
                (BATTERY / f"{clip}.{scorer}.txt").write_text(t)
            except Exception as e:
                print(f"  {clip:<18}{scorer:<10}{type(e).__name__}: {str(e)[:30]}")

    print("\n  Transcripts written next to each clip as <clip>.<scorer>.txt")
    print("  NOTE: ASR recovery is a proxy. It is not human comprehension, and the")
    print("        eval reports both. Still listen to 3a vs 3c.")


def main() -> int:
    print("Credential smoke test — real calls, not presence checks\n")
    missing = [k for k in ("RIME_API_KEY", "SARVAM_API_KEY", "DEEPGRAM_API_KEY",
                           "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
                           "OPENROUTER_API_KEY") if not os.environ.get(k)]
    if missing:
        print(f"  not set: {', '.join(missing)}\n")

    check_rime()
    check_sarvam()
    check_deepgram()
    check_livekit()
    check_openrouter()

    if results.get("Sarvam") or results.get("Deepgram"):
        check_gate()

    bad = [k for k, v in results.items() if not v]
    print(f"\n{len(results) - len(bad)}/{len(results)} credentials working"
          + (f" — failing: {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
