"""Pre-render the borrower page's spoken Hindi prompts through Rime.

    make prompts          # needs RIME_API_KEY; writes web/prompts/*.wav
    uv run python -m web.prompts --list    # print the strings, no network

The borrower has to be told "tap the green button" and "allow the microphone" BEFORE any
agent is on the line, and she cannot be told in writing — that is the premise of the whole
product. So these six strings are synthesized once, by the same voice
(coda/taru/hi, 8 kHz mu-law) that is about to read her loan, and served as static files.

Every string here is pre-call. Nothing in this file duplicates something the agent says:
`agent/main.py` speaks the consent outcome itself, and two voices saying two different
sentences at the same moment is worse than no audio at all.

Output is 8 kHz 16-bit PCM in a WAV container — Rime's mu-law decoded with the same
`ulaw_to_pcm16` the agent uses, because browsers will not play raw G.711. The band-limited
telephone character survives, which is the point: the prompts should sound like the call.

The page degrades to the browser's own speechSynthesis when a file is missing, so running
this is optional for development and expected for a demo.

⚠️ The Hindi here was written by an agent and NO NATIVE SPEAKER HAS REVIEWED IT.
`TALK_SCRIPT.md:118` says the same of the clause text and calls it the biggest unknown in
the project. Listen to these before showing them to anyone.
"""

from __future__ import annotations

import asyncio
import sys
import wave
from pathlib import Path

OUT = Path(__file__).parent / "prompts"

# key -> (Hindi, what it is for)
PROMPTS: dict[str, tuple[str, str]] = {
    "ready": (
        "नमस्ते। आपके लोन की ज़रूरी जानकारी तैयार है। सुनने के लिए हरा बटन दबाइए।",
        "Screen 0. The first tap is also the gesture that unblocks audio on mobile.",
    ),
    "mic": (
        # "Allow" stays in English on purpose: that is the literal word she has to find on
        # the operating system's own dialog, and the Hindi for it would not help her.
        "अब माइक चालू करने की अनुमति दीजिए। Allow दबाइए।",
        "Screen 1, before getUserMedia.",
    ),
    "mic_denied": (
        "माइक के बिना हम आपकी बात रिकॉर्ड नहीं कर सकते। कृपया फिर से कोशिश कीजिए।",
        "Screen 1, permission refused. Amber, not red — this is recoverable.",
    ),
    "insecure": (
        "यह पेज सुरक्षित नहीं है, इसलिए माइक काम नहीं करेगा। कृपया अपने प्रतिनिधि को बताइए।",
        "window.isSecureContext is false. Said ALOUD because a written warning is useless "
        "here, and because a silently dead microphone looks like a broken product.",
    ),
    "connecting": (
        "जोड़ रहे हैं। कृपया थोड़ा इंतज़ार कीजिए।",
        "Between the tap and the agent's first word.",
    ),
    "waiting_agent": (
        "प्रतिनिधि जुड़ रहे हैं। लाइन पर बने रहिए।",
        "Room joined but no agent yet — usually the worker is not running.",
    ),
}


def write_wav(path: Path, ulaw: bytes) -> float:
    """mu-law bytes -> an 8 kHz 16-bit PCM WAV. Returns duration in seconds."""
    from agent.session import ulaw_to_pcm16  # noqa: PLC0415

    pcm = ulaw_to_pcm16(ulaw)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(pcm)
    # mu-law at 8 kHz is 1 byte per sample, so this is exact.
    return len(ulaw) / 8000.0


async def main() -> int:
    if "--list" in sys.argv:
        for key, (text, why) in PROMPTS.items():
            print(f"{key:<14} {text}\n{'':<14} {why}\n")
        return 0

    from delivery import rime_ws3  # noqa: PLC0415

    total = 0.0
    for key, (text, _) in PROMPTS.items():
        try:
            result = await rime_ws3.synthesize([text])
        except Exception as e:  # noqa: BLE001
            print(f"  {key:<14} FAILED: {type(e).__name__}: {e}")
            print("\nRime is US-only; a connect from India occasionally times out. Re-run.")
            return 1
        seconds = write_wav(OUT / f"{key}.wav", bytes(result.audio))
        total += seconds
        print(f"  {key:<14} {seconds:5.1f}s  {text}")

    print(f"\nwrote {len(PROMPTS)} prompts, {total:.1f}s of audio, to {OUT}")
    print("NOW LISTEN TO THEM. No native Hindi speaker has reviewed this wording.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
