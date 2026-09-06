"""The active provider, as one dict, from the same constants the delivery layer sends.

The submission requires that the active provider be observable on screen and that fallback
behaviour be disclosed. That is only true if the badge reads the SAME constants the socket
does — a hand-typed "Rime coda / taru" in the HTML would keep displaying `coda` on the day
someone changes the request and would be worse than showing nothing.

`modelId` is the one that matters: it defaults to `mistv3`, which has no Hindi at all, and
an unrecognised value is also served by mistv3 rather than erroring. The badge is the
last place a human can catch that before a demo.
"""

from __future__ import annotations

import os

from delivery import rime_ws3

# Sarvam leads Hindi telephony WER (5.0 vs Deepgram 13.0) and is native 8 kHz. Deepgram is
# the disclosed fallback and is pinned to `hi`, never `multi` — `multi` has a documented,
# staff-acknowledged Hindi->Spanish misdetection on Hinglish calls, which on this product
# would silently destroy teach-back grading.
# The badge must state what is ACTUALLY running, not what we intended to run. This said
# `saaras:v4-realtime` until it was checked against the code path: livekit-plugins-sarvam
# 1.8.0 hardcodes REALTIME_MODEL = "saaras:v3-realtime" (stt_streaming.py:53), exposes no
# `model` parameter, and raises ValueError on any other value. agent/session.py therefore
# runs v3-realtime. A badge claiming v4 would have misreported the active provider to
# anyone reading the screen.
_STT = {
    "sarvam": {"vendor": "Sarvam", "model": "saaras:v3-realtime", "language": "hi-IN",
               "mode": "codemix", "endpointing": "vad"},
    "deepgram": {"vendor": "Deepgram", "model": "nova-3", "language": "hi",
                 "mode": "-", "endpointing": "-"},
}

FALLBACK_DISCLOSURE = (
    "Fallback: Deepgram nova-3, language=hi (never multi). Not active unless shown above. "
    "Rime Coda is the only Rime model with Hindi — there is no TTS fallback."
)

CHANNEL_DISCLOSURE = (
    "Simulated telephone channel: 8 kHz mu-law, band-limited 300-3400 Hz. "
    "No live PSTN leg has been validated."
)


def active() -> dict:
    name = os.environ.get("STT_PROVIDER", "sarvam").lower()
    return {
        "tts": {
            "vendor": "Rime",
            "transport": "/ws3",
            "model_id": rime_ws3.MODEL_ID,
            "speaker": rime_ws3.SPEAKER,
            "lang": rime_ws3.LANG,
            "audio_format": rime_ws3.AUDIO_FORMAT,
            "sample_rate": rime_ws3.SAMPLE_RATE,
            "time_scale_factor": 1.0,  # speedAlpha is never sent; see PLAN.md
            "segment": "never",
        },
        "stt": _STT.get(name, _STT["sarvam"]) | {"selected_by": "STT_PROVIDER"},
        "fallback": FALLBACK_DISCLOSURE,
        "channel": CHANNEL_DISCLOSURE,
    }
