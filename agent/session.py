"""LiveKit wiring for the consent call. livekit-agents 1.8.0 shape.

Read the installed source, not a tutorial: 1.8.0 changed the entrypoint
(`AgentServer` + `@server.rtc_session()`) and collapsed every flat barge-in kwarg into one
`turn_handling=TurnHandlingOptions(...)` object.

FOUR DEFAULTS THAT SILENTLY BREAK A CONSENT PRODUCT — all set explicitly below:

  * `resume_false_interruption=True` would resume reading a clause our FSM has already
    closed out as PARTIALLY_HEARD. Our rule is re-read from the start, always. Off.
  * `backchannel_boundary=(1.0, 1.0)` suppresses overlapping speech within a second of a
    turn boundary as a backchannel. A borrower's "हाँ" right after a rupee amount is
    exactly that — so Stress B would never register. Set to (0, 0); agent/rushed_consent.py
    classifies the utterance instead.
  * `rime.TTS(use_websocket=False)` is the default and silently sets streaming=False AND
    aligned_transcript=False. No error, no warning.
  * An interruption before the first frame plays leaves `forwarded_text=""` and the chat
    item is DROPPED — no event at all. `PlayoutLedger` therefore defaults a missing event
    to zero seconds, never to "it finished".

Two more deliberate choices, both measured rather than assumed:

  * `synchronized_transcript` is NOT used. Without word timestamps it estimates position
    with Latin-script syllable counting applied to Devanagari. `playback_position` is a
    real measured playout figure; the synchronizer in Hindi is a guess.
  * Clause audio does not come from the Rime LiveKit plugin. The plugin hardcodes
    `audioFormat=pcm` on its /ws3 query, and this product needs 8 kHz mu-law so the demo
    channel is byte-identical to the eval channel — and so segment duration is an exact
    byte count. We synthesize each flush segment through delivery/rime_ws3.py and hand the
    frames to `session.say(text, audio=...)`, which is the supported hook for
    caller-supplied audio.

The LiveKit secret in .env is a masked placeholder, so this file has NOT been run against a
live room. Everything in it that can be verified without a network is verified in
tests/test_agent.py: the mu-law decoder against ffmpeg, the playout ledger, and the whole
clause loop against a fake session.

Self-check (no network):  uv run python -m agent.session
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import websockets
from livekit import rtc
from livekit.agents import Agent, AgentSession, stt
from livekit.agents.voice.io import PlaybackFinishedEvent
from livekit.plugins import rime

from agent.consent_fsm import ConsentFSM, jsonl_sink
from agent.rushed_consent import ConsentDecision, evaluate
from agent.teachback import grade
from delivery import rime_ws3
from kfs.clauses import Clause

SAMPLE_RATE = rime_ws3.SAMPLE_RATE  # 8000 — telephony, and the eval's channel
FRAME_MS = 20

# TurnHandlingOptions is a TypedDict in 1.8.0, so this is just a dict — but every key here
# is a default we are overriding on purpose. Do not trim it.
TURN_HANDLING: dict = {
    "interruption": {
        "enabled": True,
        "mode": "vad",  # 'adaptive' needs the inference service; vad is local and honest
        "min_duration": 0.2,  # a borrower's "रुको" is short; the 0.5 default eats it
        "resume_false_interruption": False,
        "backchannel_boundary": (0, 0),
    },
    "preemptive_generation": {"enabled": False},  # never synthesize a clause we may retract
}


# ------------------------------------------------------------------ audio plumbing

def ulaw_to_pcm16(payload: bytes) -> bytes:
    """G.711 mu-law -> little-endian int16 PCM.

    Hand-rolled because `audioop` was removed from the stdlib in 3.13 (PEP 594) and every
    snippet on the internet uses it. Vectorised over the whole buffer; verified sample-for-
    sample against ffmpeg's decoder in tests/test_agent.py.
    """
    u = ~np.frombuffer(payload, dtype=np.uint8).astype(np.int32) & 0xFF
    magnitude = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 0x07)
    pcm = np.where(u & 0x80, 0x84 - magnitude, magnitude - 0x84)
    return np.clip(pcm, -32768, 32767).astype("<i2").tobytes()


def frames_from_ulaw(payload: bytes, *, frame_ms: int = FRAME_MS) -> Iterator[rtc.AudioFrame]:
    """Slice mu-law bytes into LiveKit frames. 1 byte per sample, so slicing is exact."""
    pcm = ulaw_to_pcm16(payload)
    step = SAMPLE_RATE * frame_ms // 1000
    for i in range(0, len(pcm) // 2, step):
        chunk = pcm[i * 2 : (i + step) * 2]
        yield rtc.AudioFrame(chunk, SAMPLE_RATE, 1, len(chunk) // 2)


async def _aiter(frames: Iterator[rtc.AudioFrame]):
    for f in frames:
        yield f


# ------------------------------------------------------------------ playout ledger

class PlayoutLedger:
    """Collects PlaybackFinishedEvent — the only trustworthy source of "what was heard".

    `playback_position` is audio actually played out, not generated. The default when no
    event arrives is 0.0, and that default is the point: LiveKit drops the chat item
    entirely if a speech is interrupted before its first frame, so silence from the
    framework means the borrower heard nothing.
    """

    def __init__(self) -> None:
        self.events: list[PlaybackFinishedEvent] = []

    def attach(self, audio_output) -> None:
        # Safe against a race with SpeechHandle.wait_for_playout(): AudioOutput emits this
        # event synchronously AFTER setting the internal asyncio.Event, so the listener has
        # already run by the time any awaiting coroutine resumes. (io.py, on_playback_finished.)
        audio_output.on("playback_finished", self.events.append)

    def take(self) -> tuple[float, bool]:
        """Consume the events seen since the last call: (seconds played, interrupted)."""
        evs, self.events = self.events, []
        if not evs:
            return 0.0, True  # no event means heard nothing
        return sum(e.playback_position for e in evs), any(e.interrupted for e in evs)


# ------------------------------------------------------------------ Rime socket pool

class RimeSocketPool:
    """One live /ws3 socket plus a warm spare, because there is no cancel primitive.

    Measured: `{"operation":"clear"}` does NOT cancel in-flight synthesis and does not drop
    text already flushed, and `contextId` is not a fence (it came back null on every frame).
    The only hard stop is closing the socket. Opening a fresh one costs a round trip to US
    West — 250-350 ms from India — so a spare is kept connected and promoted on barge-in,
    and that latency lands on the reconnect, not on the borrower.
    """

    def __init__(self, *, lang: str = rime_ws3.LANG, speaker: str = rime_ws3.SPEAKER) -> None:
        self._url = rime_ws3.url(lang=lang, speaker=speaker)
        self._live: websockets.ClientConnection | None = None
        self._spare: websockets.ClientConnection | None = None

    async def _open(self) -> websockets.ClientConnection:
        return await websockets.connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {rime_ws3.api_key()}"},
            max_size=None,
        )

    async def start(self) -> None:
        self._live = await self._open()
        self._spare = await self._open()

    async def synthesize(self, text: str, *, timeout: float = 30.0) -> bytes:
        """One text -> one flush -> the mu-law bytes for exactly that segment."""
        if self._live is None:
            await self.start()
        assert self._live is not None
        return await rime_ws3_flush(self._live, text, timeout=timeout)

    async def hard_stop(self) -> None:
        """Barge-in: clear, close, promote the spare, open a new spare.

        `clear` is sent first even though it does not cancel anything, because it does drop
        text not yet flushed and costs nothing. The close is what actually stops audio.
        """
        old, self._live, self._spare = self._live, self._spare, None
        if old is not None:
            try:
                await old.send(json.dumps({"operation": "clear"}))
            except Exception:
                pass  # already dead; the close below is the real stop
            await old.close()
        if self._live is None:
            self._live = await self._open()
        self._spare = await self._open()

    async def aclose(self) -> None:
        for ws in (self._live, self._spare):
            if ws is not None:
                await ws.close()
        self._live = self._spare = None


async def rime_ws3_flush(ws, text: str, *, timeout: float = 30.0) -> bytes:
    """Send one text + flush on an already-open socket, collect until the segment ends.

    Split out from delivery/rime_ws3.synthesize() (which owns its own connection) so the
    pool above can keep a socket alive across segments. Same frame handling, same
    base64-in-JSON chunk shape confirmed by the day-1 battery.
    """
    import base64

    await ws.send(json.dumps({"text": text}))
    await ws.send(json.dumps({"operation": "flush"}))

    out = bytearray()
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(raw, bytes):
            continue  # /ws3 sends JSON; binary frames are /ws
        msg = json.loads(raw)
        kind = msg.get("type")
        if kind == "chunk":
            out.extend(base64.b64decode(msg.get("data") or msg.get("audioContent") or ""))
        elif kind in ("flush_done", "segment_done", "done", "eos"):
            return bytes(out)
        elif kind == "error":
            raise RuntimeError(f"rime error: {msg}")


# ------------------------------------------------------------------ provider factories

def build_stt() -> stt.STT:
    """`livekit.agents.stt.STT` is already the provider interface. One factory, no wrapper.

    Sarvam leads Hindi telephony WER (5.0 vs Deepgram 13.0) and `codemix` keeps English
    loan words in Latin script, which is what the teach-back grader wants. Deepgram is the
    fallback at language="hi" — NEVER "multi", which has a documented Hindi->Spanish
    misdetection on Hinglish calls.
    """
    provider = os.environ.get("STT_PROVIDER", "sarvam").lower()

    if provider == "sarvam":
        from livekit.plugins import sarvam

        # NOTE: livekit-plugins-sarvam 1.8.0 pins saaras:v3-realtime and rejects any other
        # model in RealtimeSTTOptions.__post_init__. v4-realtime is not reachable through
        # this plugin — verified in the installed source, not assumed.
        return sarvam.STTRealtime(
            language="hi-IN",
            mode="codemix",
            endpointing="vad",
            encoding="mulaw",
            sample_rate=SAMPLE_RATE,
        )

    if provider == "deepgram":
        from livekit.plugins import deepgram

        return deepgram.STT(model="nova-3", language="hi", sample_rate=SAMPLE_RATE)

    raise ValueError(f"STT_PROVIDER must be 'sarvam' or 'deepgram', got {provider!r}")


def build_tts() -> rime.TTS:
    """Rime for the agent's own conversational turns (prompts, questions, refusals).

    Clause audio does NOT come through here — see the module docstring. `use_websocket=True`
    is not optional: the default silently disables streaming and aligned transcripts.
    `modelId` is pinned because it defaults to mistv3, which has no Hindi at all.
    """
    return rime.TTS(
        model="coda",
        speaker=rime_ws3.SPEAKER,
        lang=rime_ws3.LANG,
        use_websocket=True,
        segment="immediate",  # we control utterance boundaries, not the server
        time_scale_factor=1.0,  # never speedAlpha: the three-way speed trap
    )


# ------------------------------------------------------------------ the clause loop

@dataclass
class ClauseOutcome:
    clause_id: str
    state: str
    played_s: float
    interrupted: bool
    teach_back: dict | None = None


class ConsentAgent(Agent):
    """Reads the KFS. Deliberately has no tools and no free-form generation.

    Every number the borrower hears comes from the extracted schema through the delivery
    layer. An LLM that can improvise a loan term into a consent call is a liability, so
    this agent only speaks text we constructed.
    """

    def __init__(self, instructions: str = "") -> None:
        super().__init__(instructions=instructions or _INSTRUCTIONS)


_INSTRUCTIONS = (
    "You read a regulatory Key Facts Statement aloud in Hindi and check understanding. "
    "You never state a number that was not given to you verbatim. You never reassure, "
    "estimate, or summarise a financial term."
)


async def deliver_clause(session: AgentSession, fsm: ConsentFSM, clause: Clause,
                         pool: RimeSocketPool | None, ledger: PlayoutLedger) -> ClauseOutcome:
    """Read one clause, segment by segment, and record exactly what played.

    Segments are synthesized lazily, one at a time. A barge-in during segment k therefore
    means segments k+1.. were never requested, so no stale audio can arrive for them — the
    only thing needing a hard stop is the socket mid-segment.
    """
    fsm.begin_delivery(clause.id)
    ledger.take()  # discard anything left over from the previous clause

    played = 0.0
    interrupted = False

    for seg in clause.segments:
        if pool is not None and not seg.audio:
            seg.audio = await pool.synthesize(seg.text)

        handle = session.say(seg.text, audio=_aiter(frames_from_ulaw(seg.audio)),
                             allow_interruptions=True)
        await handle.wait_for_playout()

        seg_played, seg_interrupted = ledger.take()
        played += seg_played
        if seg_interrupted or handle.interrupted:
            interrupted = True
            if pool is not None:
                await pool.hard_stop()
            break

    state = fsm.end_delivery(clause.id, played_s=played, interrupted=interrupted)
    return ClauseOutcome(clause.id, state.value, round(played, 3), interrupted)


async def run_teach_back(session: AgentSession, fsm: ConsentFSM, clause: Clause,
                         question: str, answers: asyncio.Queue[str],
                         timeout: float = 30.0) -> dict:
    """Ask the borrower to explain the clause back, grade it, apply it to the FSM."""
    await session.say(question).wait_for_playout()
    try:
        transcript = await asyncio.wait_for(answers.get(), timeout=timeout)
    except TimeoutError:
        transcript = ""

    result = grade(clause, transcript)
    fsm.teach_back(clause.id, transcript=transcript, passed=result.passed,
                   grades=result.as_dicts())
    return {"transcript": transcript, "graded_by": result.graded_by,
            "passed": result.passed, "grades": result.as_dicts(), "note": result.note}


async def run_consent_flow(session: AgentSession, fsm: ConsentFSM, clauses: list[Clause],
                           pool: RimeSocketPool | None, ledger: PlayoutLedger,
                           answers: asyncio.Queue[str], *,
                           max_attempts: int = 2) -> ConsentDecision | None:
    """The whole call: read, check, re-read on failure, then gate the consent utterance."""
    for clause in clauses:
        for _ in range(max_attempts):
            await deliver_clause(session, fsm, clause, pool, ledger)
            if fsm.state(clause.id).value != "HEARD":
                continue  # PARTIALLY_HEARD -> re-read from the START, never resume
            await run_teach_back(session, fsm, clause,
                                 f"{clause.title_hi} के बारे में आपने क्या समझा?", answers)
            if fsm.state(clause.id).value == "UNDERSTOOD":
                break

    finished_at = time.monotonic()
    await session.say("क्या आप इन शर्तों पर सहमत हैं?").wait_for_playout()

    try:
        utterance = await asyncio.wait_for(answers.get(), timeout=60.0)
    except TimeoutError:
        return None

    return evaluate(fsm, utterance, latency_s=time.monotonic() - finished_at)


def wire_transcripts(session: AgentSession) -> asyncio.Queue[str]:
    """Final borrower transcripts, in order. Interim results are ignored on purpose."""
    q: asyncio.Queue[str] = asyncio.Queue()

    @session.on("user_input_transcribed")
    def _on(ev) -> None:
        if ev.is_final and ev.transcript.strip():
            q.put_nowait(ev.transcript.strip())

    return q


def build_session(*, llm=None) -> AgentSession:
    """The session, with the four dangerous defaults set before anything is built on them."""
    return AgentSession(
        stt=build_stt(),
        tts=build_tts(),
        llm=llm,
        turn_handling=TURN_HANDLING,
        # Not use_tts_aligned_transcript: Rime emits no word timestamps for Hindi and the
        # synchronizer would fall back to Latin syllable counting over Devanagari.
        use_tts_aligned_transcript=False,
    )


def new_fsm(clauses: list[Clause], call_id: str) -> ConsentFSM:
    return ConsentFSM(clauses, call_id=call_id, sink=jsonl_sink(call_id))


def _demo() -> None:
    """Offline self-check: the audio path and the option block, with no network."""
    import math

    tone = bytes((int(127 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE)) + 128) % 256
                 for i in range(SAMPLE_RATE))
    frames = list(frames_from_ulaw(tone))
    assert len(frames) == SAMPLE_RATE // (SAMPLE_RATE * FRAME_MS // 1000)
    assert sum(f.samples_per_channel for f in frames) == SAMPLE_RATE

    ledger = PlayoutLedger()
    assert ledger.take() == (0.0, True), "no event MUST mean heard nothing"
    ledger.events.append(PlaybackFinishedEvent(playback_position=1.25, interrupted=True))
    assert ledger.take() == (1.25, True)

    i = TURN_HANDLING["interruption"]
    assert i["resume_false_interruption"] is False
    assert i["backchannel_boundary"] == (0, 0)

    print(f"  {len(frames)} frames of {FRAME_MS} ms from 1.000 s of mu-law")
    print(f"  interruption options: {i}")
    print("\nsession audio path + dangerous defaults OK")


if __name__ == "__main__":
    _demo()
