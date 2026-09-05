"""The day-1 test battery. Run this BEFORE writing product code.

Six experiments, ~45 minutes including listening. The whole plan branches on them, and
each one is a project-killer if assumed rather than measured:

  1. Devanagari vs romanized  — the docs say NOTHING about script. Everything downstream
                                (normaliser output alphabet, teach-back matching) depends
                                on which one Coda's Hindi voices actually want.
  2. lang omitted             — the only documented route to timestamps on a Hindi voice.
                                Assume it fails; verify rather than assume.
  3. THE GATE                 — raw vs normalised rupee amount. If there is no audible
                                gap, the headline claim pivots TONIGHT, not on day 3.
  4. digit-run formatting     — docs say bare runs read digit-by-digit and comma-grouped
                                read as quantities. That inverts by formatting alone.
  5. spell() on Coda          — Rime's own docs contradict each other. Cheap to settle.
  6. percentage + tenure      — the classes no library covers.

Writes clips to evals/results/battery/ and a protocol transcript to frames.json.

    uv run python scripts/battery.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from delivery import rime_ws3  # noqa: E402
from delivery.normalizer.hindi import digits, percent, rupees, tenure_months  # noqa: E402

OUT = Path("evals/results/battery")

# One carrier sentence, reused so the only variable is the value's rendering.
CARRIER = "आपके लोन की कुल राशि {} है।"

TESTS: list[tuple[str, str, str]] = [
    # (id, description, text)
    ("1a_devanagari", "Test 1: Devanagari input", "आपका ब्याज दर चौदह प्रतिशत है।"),
    ("1b_romanized", "Test 1: romanized input", "Aapka byaj dar chaudah pratishat hai."),

    ("3a_raw_grouped", "Test 3 GATE: raw, Indian grouping", CARRIER.format("₹1,04,596")),
    ("3b_raw_plain", "Test 3 GATE: raw, no separators", CARRIER.format("104596")),
    ("3c_normalized", "Test 3 GATE: normalised", CARRIER.format(rupees("₹1,04,596"))),

    ("4a_bare_run", "Test 4: bare 10-digit run", "आपका खाता नंबर 9157114007 है।"),
    ("4b_grouped_run", "Test 4: comma-grouped", "आपका खाता नंबर 9,157,114,007 है।"),
    ("4c_normalized", "Test 4: digit-by-digit", f"आपका खाता नंबर {digits('9157114007')} है।"),

    ("5_spell", "Test 5: spell() passthrough on Coda", "आपका कोड spell(A1B2) है।"),

    ("6a_pct_raw", "Test 6: raw percentage", "वार्षिक ब्याज दर 18.5% है।"),
    ("6b_pct_norm", "Test 6: normalised percentage", f"वार्षिक ब्याज दर {percent('18.5')} है।"),
    ("6c_tenure_raw", "Test 6: raw tenure", "अवधि 36 महीने है।"),
    ("6d_tenure_norm", "Test 6: normalised tenure", f"अवधि {tenure_months(36)} है।"),
]


def to_wav(ulaw: bytes, dest: Path) -> None:
    """Wrap raw 8 kHz mu-law as a playable WAV via ffmpeg.

    scipy.io.wavfile cannot read WAVE_FORMAT_MULAW, and audioop is gone from the stdlib in
    3.13 (PEP 594) — so ffmpeg does the codec work and Python never touches mu-law bytes.
    """
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "mulaw", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
         "-c:a", "pcm_s16le", str(dest)],
        input=ulaw, check=True,
    )


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    transcript: dict[str, object] = {}

    print("Test 2: does omitting `lang` yield timestamps on a Hindi voice?")
    print("  (the only documented loophole — expected to fail, verifying anyway)")
    for label, lang in [("lang=hi", "hi"), ("lang omitted", None)]:
        try:
            r = await rime_ws3.synthesize(["आपका ब्याज दर चौदह प्रतिशत है।"], lang=lang)
            print(f"  {label:>14}: timestamps={r.got_timestamps}  audio={r.duration_s:.2f}s")
            to_wav(r.audio, OUT / f"2_{'hi' if lang else 'nolang'}.wav")
            transcript[f"test2_{label}"] = {"got_timestamps": r.got_timestamps,
                                            "frames": r.frames}
        except Exception as e:
            print(f"  {label:>14}: {type(e).__name__}: {e}")
            transcript[f"test2_{label}"] = {"error": f"{type(e).__name__}: {e}"}

    print("\nSynthesising battery clips...")
    for tid, desc, text in TESTS:
        try:
            r = await rime_ws3.synthesize([text])
            to_wav(r.audio, OUT / f"{tid}.wav")
            print(f"  {tid:<16} {r.duration_s:5.2f}s  {desc}")
            transcript[tid] = {"text": text, "duration_s": r.duration_s,
                               "frames": r.frames}
        except Exception as e:
            print(f"  {tid:<16}  FAILED  {type(e).__name__}: {e}")
            transcript[tid] = {"text": text, "error": f"{type(e).__name__}: {e}"}

    (OUT / "frames.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2))

    print(f"\nClips in {OUT}/  — protocol transcript in {OUT}/frames.json")
    print("\nNOW LISTEN, in this order. You are the instrument here:")
    print("  1a vs 1b   Does Coda read Devanagari correctly, or does it need romanized?")
    print("  3a vs 3c   THE GATE. Is the raw amount mangled and the normalised one clean?")
    print("  4a vs 4b   Does formatting alone flip digit-by-digit vs quantity?")
    print("  6a vs 6b   Is '18.5%' read as a percentage at all?")
    print("\nIf 3a sounds fine, the headline claim pivots tonight — not on day 3.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
