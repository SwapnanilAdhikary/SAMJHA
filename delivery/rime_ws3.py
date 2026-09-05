"""Minimal Rime /ws3 client.

Transport facts this is built around, from docs.rime.ai (verified 2026-09-05):

  * `modelId` DEFAULTS TO mistv3, which has no Hindi at all. Always pinned explicitly.
    An unrecognised value is also served by mistv3 — so a typo yields working audio from
    the wrong model rather than an error.
  * Audio arrives base64-encoded inside JSON `chunk` events. (Binary frames are /ws, not
    /ws3.) ~33% bandwidth overhead, irrelevant at our volume.
  * Word-level `timestamps` events are emitted ONLY for lang en/eng, es/spa, or when lang
    is omitted. A Hindi request gets chunk + done and NO timestamps event AND NO ERROR.
    This is why the consent mechanic uses playout position over known segment durations
    instead of vendor word timings.
  * There is NO cancel primitive. `clear` does not cancel in-flight synthesis and does not
    drop text already flushed. Hard stop = close the socket.
  * `contextId` is NOT a fence: "Rime does not maintain multiple simultaneous context IDs."
    Send two texts before synthesis starts and only the later id appears on the first
    chunk. Do not build discard logic on it.
  * `segment=never` + explicit flush means WE own utterance boundaries — which is what
    makes per-segment duration accounting exact.

The send-message schema is NOT yet verified against a live socket (we had no key when this
was written), so `synthesize()` records every raw frame it receives. Run the battery once
and the transcript tells you the real shape.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlencode

import websockets

WS3 = "wss://users-ws.rime.ai/ws3"

MODEL_ID = "coda"
SPEAKER = "taru"
LANG = "hi"

# 8 kHz mu-law is the telephony arm and the demo arm both — so the audio a judge hears is
# the same channel the eval measures.
AUDIO_FORMAT = "mulaw"
SAMPLE_RATE = 8000

# mu-law is 1 byte per sample, so byte count converts to duration exactly:
# 8000 bytes = 1.000 s. No estimation anywhere in the playout clock.
BYTES_PER_SECOND = {"mulaw": SAMPLE_RATE}


@dataclass
class Segment:
    """One flush unit. `carries_key_value` is what the consent FSM gates on."""

    text: str
    carries_key_value: bool = False
    audio: bytearray = field(default_factory=bytearray)

    @property
    def duration_s(self) -> float:
        return len(self.audio) / BYTES_PER_SECOND[AUDIO_FORMAT]


@dataclass
class Result:
    segments: list[Segment]
    frames: list[dict]  # every raw frame, for protocol discovery and debugging
    got_timestamps: bool

    @property
    def audio(self) -> bytes:
        return b"".join(bytes(s.audio) for s in self.segments)

    @property
    def duration_s(self) -> float:
        return len(self.audio) / BYTES_PER_SECOND[AUDIO_FORMAT]


def url(*, lang: str | None = LANG, speaker: str = SPEAKER, model_id: str = MODEL_ID,
        audio_format: str = AUDIO_FORMAT, sample_rate: int = SAMPLE_RATE) -> str:
    """Build the connect URL. `lang=None` omits the param — the documented (and only)
    route to timestamps on a non-English voice, which the battery tests."""
    q = {
        "speaker": speaker,
        "modelId": model_id,  # never omit: defaults to mistv3, no Hindi
        "audioFormat": audio_format,
        "samplingRate": sample_rate,
        "segment": "never",  # we own the boundaries
    }
    if lang is not None:
        q["lang"] = lang
    return f"{WS3}?{urlencode(q)}"


def api_key() -> str:
    key = os.environ.get("RIME_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "RIME_API_KEY is not set.\n"
            "  Sign up (free, no card): https://app.rime.ai/signup\n"
            "  Then: echo 'RIME_API_KEY=...' >> .env"
        )
    return key


async def synthesize(texts: list[str], *, lang: str | None = LANG,
                     speaker: str = SPEAKER, timeout: float = 60.0,
                     key_value_flags: list[bool] | None = None) -> Result:
    """Send each text as its own flush segment; return per-segment audio.

    One text per flush is the whole design: it makes "did the segment carrying the number
    finish playing?" exact arithmetic rather than an estimate.
    """
    flags = key_value_flags or [False] * len(texts)
    segments = [Segment(text=t, carries_key_value=f) for t, f in zip(texts, flags)]
    frames: list[dict] = []
    got_timestamps = False
    idx = 0

    async with websockets.connect(
        url(lang=lang, speaker=speaker),
        additional_headers={"Authorization": f"Bearer {api_key()}"},
        max_size=None,
    ) as ws:
        for seg in segments:
            await ws.send(json.dumps({"text": seg.text}))
            await ws.send(json.dumps({"operation": "flush"}))
        await ws.send(json.dumps({"operation": "eos"}))

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                frames.append({"type": "__timeout__"})
                break

            if isinstance(raw, bytes):  # unexpected on /ws3; record rather than guess
                frames.append({"type": "__binary__", "bytes": len(raw)})
                continue

            msg = json.loads(raw)
            # Record everything except the audio payload, which would swamp the log.
            frames.append({k: v for k, v in msg.items() if k not in ("data", "audioContent")})

            kind = msg.get("type")
            if kind == "chunk":
                payload = msg.get("data") or msg.get("audioContent") or ""
                if idx < len(segments):
                    segments[idx].audio.extend(base64.b64decode(payload))
            elif kind == "timestamps":
                got_timestamps = True
            elif kind in ("flush_done", "segment_done"):
                idx = min(idx + 1, len(segments) - 1)
            elif kind in ("done", "eos", "error"):
                break

    return Result(segments=segments, frames=frames, got_timestamps=got_timestamps)
