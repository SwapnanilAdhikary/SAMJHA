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
import contextlib
import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlencode

import websockets
from websockets.exceptions import WebSocketException

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


CONNECT_ATTEMPTS = 3
OPEN_TIMEOUT = 10.0

# Everything that means "the edge did not give us a socket this time". Rime is US-only
# with no India region, and all of these have been seen from here mid-call: a
# trans-Pacific handshake timeout, and an HTTP 502 from the edge (which arrives as
# InvalidStatus, a WebSocketException). None of them is a reason to end a consent call.
#
# Imported explicitly rather than reached through `websockets.exceptions`: that is a LAZY
# submodule, so touching it at module scope raises AttributeError at import time. It works
# inside a function body, which is why the `except` clause below gets away with it.
TRANSIENT = (TimeoutError, OSError, WebSocketException)


async def connect_with_retry(*, lang: str | None = LANG, speaker: str = SPEAKER,
                             attempts: int = CONNECT_ATTEMPTS,
                             open_timeout: float = OPEN_TIMEOUT):
    """Open a /ws3 socket, retrying the handshake with escalating backoff.

    The retry policy lives here so the batch helper and `agent.session.RimeSocketPool`
    cannot drift apart. The pool used to call `websockets.connect` directly with no
    retry, and a single HTTP 502 while re-opening its warm spare crashed a live call
    three clauses in.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await websockets.connect(
                url(lang=lang, speaker=speaker),
                additional_headers={"Authorization": f"Bearer {api_key()}"},
                max_size=None,
                open_timeout=open_timeout,
            )
        except TRANSIENT as e:
            last = e
            if attempt < attempts:
                # Short, escalating. Long enough to ride out a blip, short enough that a
                # listener on the line does not think the call dropped.
                await asyncio.sleep(0.5 * attempt)
    raise RuntimeError(
        f"Rime /ws3 refused a connection after {attempts} attempts: "
        f"{type(last).__name__}: {last}"
    ) from last


async def synthesize(texts: list[str], *, lang: str | None = LANG,
                     speaker: str = SPEAKER, timeout: float = 60.0,
                     key_value_flags: list[bool] | None = None,
                     attempts: int = 3) -> Result:
    """Send each text as its own flush segment; return per-segment audio.

    One text per flush is the whole design: it makes "did the segment carrying the number
    finish playing?" exact arithmetic rather than an estimate.

    Retries the opening handshake. Rime's endpoint is in the US with no India region, so a
    trans-Pacific connect occasionally times out — observed live, mid-call, on clause 4 of a
    real run. A consent call that dies partway through is worse than a slow one, and the
    caller has no better recovery than trying again.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _synthesize_once(texts, lang=lang, speaker=speaker,
                                          timeout=timeout, key_value_flags=key_value_flags)
        except (TimeoutError, OSError, websockets.exceptions.WebSocketException) as e:
            last = e
            if attempt < attempts:
                # Short, escalating backoff. Long enough to ride out a blip, short enough
                # that a listener on the line does not think the call dropped.
                await asyncio.sleep(0.5 * attempt)
    raise RuntimeError(
        f"Rime /ws3 failed after {attempts} attempts: {type(last).__name__}: {last}"
    ) from last


async def _synthesize_once(texts: list[str], *, lang: str | None, speaker: str,
                           timeout: float, key_value_flags: list[bool] | None) -> Result:
    flags = key_value_flags or [False] * len(texts)
    segments = [Segment(text=t, carries_key_value=f) for t, f in zip(texts, flags)]
    frames: list[dict] = []
    got_timestamps = False
    idx = 0

    async with websockets.connect(
        url(lang=lang, speaker=speaker),
        additional_headers={"Authorization": f"Bearer {api_key()}"},
        max_size=None,
        # 10s: long enough for a slow trans-Pacific connect, short enough that three
        # attempts still fail inside a window a listener will sit through.
        open_timeout=10,
    ) as ws:
        # ONE FLUSH AT A TIME, waiting for its `done` before sending the next text.
        #
        # This is the whole reason per-segment attribution works. /ws3 emits only `chunk`
        # frames and a `done` — there is no `flush_done` or `segment_done` event, verified
        # live and in the committed day-1 transcript (evals/results/battery/frames.json:
        # 1543 chunk, 15 done, 1 timestamps, and nothing else). An earlier version of this
        # function pipelined every text and flush and then sent `eos`, so the single
        # trailing `done` arrived after all the audio and every chunk was attributed to
        # segment 0: the first segment held the entire clause and the rest held nothing.
        #
        # That is not a cosmetic accounting error. `Clause.heard_key_values` asks "did the
        # segment carrying this value finish playing?", so a value sitting in segment 1+
        # had a zero-length segment and could never be heard — the clause read perfectly
        # to the borrower and the ledger recorded the number as missed, blocking consent
        # forever. Waiting for `done` per flush is what makes the byte counts mean what
        # the consent mechanic says they mean.
        #
        # `agent/session.rime_ws3_flush` has always done it this way on the live path;
        # this brings the batch helper into line with it.
        for seg in segments:
            await ws.send(json.dumps({"text": seg.text}))
            await ws.send(json.dumps({"operation": "flush"}))

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    frames.append({"type": "__timeout__", "segment": idx})
                    break

                if isinstance(raw, bytes):  # unexpected on /ws3; record rather than guess
                    frames.append({"type": "__binary__", "bytes": len(raw)})
                    continue

                msg = json.loads(raw)
                # Record everything except the audio payload, which would swamp the log.
                frames.append(
                    {k: v for k, v in msg.items() if k not in ("data", "audioContent")})

                kind = msg.get("type")
                if kind == "chunk":
                    payload = msg.get("data") or msg.get("audioContent") or ""
                    seg.audio.extend(base64.b64decode(payload))
                elif kind == "timestamps":
                    got_timestamps = True
                elif kind in ("done", "flush_done", "segment_done", "eos"):
                    break
                elif kind == "error":
                    raise RuntimeError(f"rime error on segment {idx}: {msg}")
            idx += 1

        # Best-effort: Rime may already be closing, and every segment's audio is in hand.
        with contextlib.suppress(Exception):
            await ws.send(json.dumps({"operation": "eos"}))

    return Result(segments=segments, frames=frames, got_timestamps=got_timestamps)
