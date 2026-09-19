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

Everything in this file that can be verified without a network is verified in
tests/test_agent.py: the mu-law decoder against ffmpeg, the playout ledger, and the whole
clause loop against a fake session. That is where the correctness that matters lives, and
it is deliberately not conditional on a room being connected.

(An earlier version of this note said the LiveKit secret in .env was a masked placeholder.
It is not — `scripts/smoke.py:117 check_livekit()` authenticates against the live project.
The claim was stale, and it had talked at least one reader out of trying a real call.)

Self-check (no network):  uv run python -m agent.session
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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
from api import events
from agent.rushed_consent import ConsentDecision, evaluate
from agent.teachback import grade
from delivery import rime_ws3
from kfs.clauses import Clause

logger = logging.getLogger("samjha.agent")

SAMPLE_RATE = rime_ws3.SAMPLE_RATE  # 8000 — telephony, and the eval's channel
FRAME_MS = 20

# TurnHandlingOptions is a TypedDict in 1.8.0, so this is just a dict — but every key here
# is a default we are overriding on purpose. Do not trim it.
# Voice barge-in depends on room acoustics we do not control. On a laptop with its own
# speakers — or on a stage with a PA — the mic hears the agent and the VAD reads that as the
# borrower, so the agent interrupts itself and every clause is chopped. Echo cancellation in
# the browser is the real fix; this is the switch for when it is not enough.
#   SAMJHA_VOICE_BARGE_IN=0  -> voice interruption off. The on-screen stop button still
#                               barges in, deterministically, and that is the same code path.
VOICE_BARGE_IN = os.environ.get("SAMJHA_VOICE_BARGE_IN", "1") != "0"

TURN_HANDLING: dict = {
    "interruption": {
        "enabled": VOICE_BARGE_IN,
        "mode": "vad",  # 'adaptive' needs the inference service; vad is local and honest
        # 0.2, not the 0.5 default: a borrower's "रुको" is short and 0.5 eats it. This was
        # briefly raised to 0.6 while chasing self-interruption that turned out to be two
        # agent jobs in one room (api/main.py post_dispatch), not echo. Echo is still a real
        # risk on a laptop, which is what the browser-side AEC and SAMJHA_VOICE_BARGE_IN
        # are for — not a blunter threshold that also deafens the agent to the borrower.
        "min_duration": 0.2,
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
        #
        # The lambda is NOT decoration. Registering `self.events.append` binds the listener
        # to that one list OBJECT; the moment take() rebound self.events to a fresh list,
        # every subsequent event went to the orphaned original and the ledger saw nothing
        # for the rest of the call. take() is called on the first line of deliver_clause to
        # drop leftovers, so the listener was orphaned before the first clause was ever
        # delivered — every clause then read in full, reported played=0.0/interrupted=True,
        # and was marked PARTIALLY_HEARD and re-read. Resolve self.events at CALL time.
        audio_output.on("playback_finished", lambda ev: self.events.append(ev))

    def take(self) -> tuple[float, bool]:
        """Consume the events seen since the last call: (seconds played, interrupted)."""
        evs = list(self.events)
        self.events.clear()  # in place: never rebind, see attach()
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
        self._lang = lang
        self._speaker = speaker
        self._url = rime_ws3.url(lang=lang, speaker=speaker)
        self._live: websockets.ClientConnection | None = None
        self._spare: websockets.ClientConnection | None = None

    async def _open(self) -> websockets.ClientConnection:
        """Retried, because the handshake fails transiently and mid-call.

        Delegated to delivery/rime_ws3.py so this and the batch helper share one retry
        policy. This used to call websockets.connect directly with no retry.
        """
        return await rime_ws3.connect_with_retry(lang=self._lang, speaker=self._speaker)

    async def start(self) -> None:
        self._live = await self._open()
        # Best-effort even at startup: a call that can speak is better than no call.
        self._spare = await self._open_spare()

    async def _open_spare(self) -> websockets.ClientConnection | None:
        """The warm spare is an OPTIMIZATION and must never end a call.

        Its only job is that the 250-350 ms reconnect after a barge-in lands on the
        reconnect rather than on the borrower. If Rime will not give us one, the next
        barge-in is slower — that is all. Losing the whole consent call instead is a
        wildly worse trade, and it is what happened live: an HTTP 502 here propagated out
        of hard_stop(), through deliver_clause and run_consent_flow, and crashed the job
        after two clauses had already been read.
        """
        try:
            return await self._open()
        except Exception as e:  # noqa: BLE001 — never fatal by design
            logger.warning("no warm Rime spare (%s: %s); the next barge-in pays the "
                           "reconnect latency", type(e).__name__, e)
            return None

    async def synthesize(self, text: str, *, timeout: float = 30.0) -> bytes:
        """One text -> one flush -> the mu-law bytes for exactly that segment.

        Shared socket, deliberately. A fresh socket per segment was tried and reverted:
        it cost up to 13.8 s of synthesis latency (two handshakes to US West on the
        critical path) and it fixed nothing, because the shared socket was never the
        problem. Measured, on both paths, every segment ends in natural silence
        (trailing-120ms RMS is 1-5% of the utterance's own RMS), so Rime is delivering
        complete audio here. Note also that Coda is NOT deterministic: the same 88-char
        text measured 9.05-10.81 s across six fresh-socket runs, a 19% spread, so duration
        alone can never tell you whether a segment was cut.
        """
        if self._live is None:
            self._live = await self._open()
        return await rime_ws3_flush(self._live, text, timeout=timeout)

    async def hard_stop(self) -> None:
        """Barge-in: clear, close, promote the spare, open a new spare.

        `clear` is sent first even though it does not cancel anything, because it does drop
        text not yet flushed and costs nothing. The close is what actually stops audio.

        Never raises. Stopping the audio is the part that matters and it has already
        happened by the time anything here can fail.
        """
        old, self._live, self._spare = self._live, self._spare, None
        if old is not None:
            with contextlib.suppress(Exception):
                await old.send(json.dumps({"operation": "clear"}))
            with contextlib.suppress(Exception):
                await old.close()  # THIS is the stop

        if self._live is None:
            # No spare to promote. Try now, but leave it None rather than raising:
            # synthesize() opens lazily, and a clause we cannot speak is recorded as
            # undelivered by deliver_clause — which keeps consent blocked, correctly.
            try:
                self._live = await self._open()
            except Exception as e:  # noqa: BLE001
                logger.warning("could not reopen the Rime socket after barge-in (%s: %s)",
                               type(e).__name__, e)

        self._spare = await self._open_spare()

    async def aclose(self) -> None:
        for ws in (self._live, self._spare):
            if ws is not None:
                with contextlib.suppress(Exception):
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
            try:
                seg.audio = await pool.synthesize(seg.text)
            except Exception as e:  # noqa: BLE001 — Rime is US-only and fails mid-call
                # A clause we could not speak was certainly not heard. Record what
                # actually played and stop, rather than letting the exception end the
                # call: the FSM leaves this clause blocking consent, which is the correct
                # outcome, and the borrower keeps a line she can be called back on.
                logger.warning("Rime failed mid-clause %s (%s: %s); recording it as "
                               "undelivered", clause.id, type(e).__name__, e)
                if played <= 0.0:
                    state = fsm.delivery_dropped(
                        clause.id, note=f"tts_failed:{type(e).__name__}")
                else:
                    # Some of it did play. `played` is the honest figure, and the segments
                    # that completed keep their heard key values.
                    state = fsm.end_delivery(clause.id, played_s=played, interrupted=True)
                return ClauseOutcome(clause.id, state.value, round(played, 3), True)

        # VOICE_BARGE_IN, not True. An explicit allow_interruptions on say() sets the
        # HANDLE's flag, and that flag is what the VAD interrupt path actually gates on
        # (agent_activity._interrupt_by_audio_activity). TURN_HANDLING["interruption"]
        # ["enabled"] only supplies the activity-level DEFAULT, so hardcoding True here
        # silently re-enabled every barge-in the kill-switch was meant to stop.
        #
        # Worse: with a per-handle True, _resolve_interruption_detection() returns None,
        # which also disarms LiveKit's own _disable_vad_interruption_soon() guard — so the
        # agent had no protection against hearing itself either. Measured in the same call:
        # the one line spoken with allow_interruptions=False ran 8.31s uncut while every
        # clause with True was chopped after ~0.2s of any mic energy.
        events.emit(fsm.call_id, "speaking", text=seg.text, kind="clause",
                    clause_id=clause.id)
        handle = session.say(seg.text, audio=_aiter(frames_from_ulaw(seg.audio)),
                             allow_interruptions=VOICE_BARGE_IN)
        await handle.wait_for_playout()

        seg_played, seg_interrupted = ledger.take()
        # The single most diagnostic line in the call. take() returns (0.0, True) when NO
        # PlaybackFinishedEvent arrived, which is indistinguishable in the record from a
        # real barge-in — so if the ledger is not wired up, every clause silently becomes
        # PARTIALLY_HEARD and the borrower can never consent to anything.
        logger.info("playout %s seg: played=%.2fs interrupted=%s handle.interrupted=%s "
                    "expected=%.2fs", clause.id, seg_played, seg_interrupted,
                    handle.interrupted, len(seg.audio) / 8000 if seg.audio else -1)
        played += seg_played
        if seg_interrupted or handle.interrupted:
            interrupted = True
            if pool is not None:
                await pool.hard_stop()
            break

    state = fsm.end_delivery(clause.id, played_s=played, interrupted=interrupted)
    return ClauseOutcome(clause.id, state.value, round(played, 3), interrupted)


async def say_narrowband(session: AgentSession, pool: RimeSocketPool | None, text: str,
                         *, allow_interruptions: bool | None = None,
                         call_id: str | None = None) -> None:
    """Speak one line through the SAME 8 kHz mu-law path the clauses use.

    Without this the call alternates sample rates mid-conversation: clause audio is our
    own /ws3 mu-law at 8 kHz, while anything handed to plain `session.say(text)` goes out
    through the rime.TTS plugin, which hardcodes `audioFormat: pcm` (RIME_EVIDENCE §5) and
    is therefore wideband. Switching between the two between a clause and the question
    about it is audible, and it sounds like a fault in the audio rather than a change of
    speaker.

    Falls back to the plugin if Rime fails here. A question she cannot hear is worse than
    a question in the wrong bandwidth, and the FSM records what actually played either way.
    """
    if allow_interruptions is None:
        allow_interruptions = VOICE_BARGE_IN
    if call_id:
        events.emit(call_id, "speaking", text=text, kind="line")
    if pool is not None:
        try:
            audio = await pool.synthesize(text)
            await session.say(text, audio=_aiter(frames_from_ulaw(audio)),
                              allow_interruptions=allow_interruptions).wait_for_playout()
            return
        except Exception as e:  # noqa: BLE001 — Rime is US-only and fails mid-call
            logger.warning("narrowband say failed (%s: %s); falling back to the plugin",
                           type(e).__name__, e)
    await session.say(text, allow_interruptions=allow_interruptions).wait_for_playout()


# How long she gets to answer. She cannot read a clock, so this number only means
# anything because the borrower page counts it down for her — see the `listening` event.
TEACH_BACK_TIMEOUT_S = 30.0


async def run_teach_back(session: AgentSession, fsm: ConsentFSM, clause: Clause,
                         question: str, answers: asyncio.Queue[str],
                         timeout: float = TEACH_BACK_TIMEOUT_S,
                         pool: RimeSocketPool | None = None) -> dict:
    """Ask the borrower to explain the clause back, grade it, apply it to the FSM."""
    # DROP EVERYTHING SAID BEFORE THE QUESTION. wire_transcripts queues every final
    # transcript for the whole call, including whatever she says while a clause is being
    # read — a "हाँ", a cough picked up as speech, the tail of her previous answer. The
    # queue is FIFO, so one stray utterance during clause 1 is served as the ANSWER to
    # clause 2, its leftover as the answer to clause 3, and so on down the call. Observed:
    #   heard_you identity   'शून्य शून्य शून्य दो चार…'   <- her real answer
    #   heard_you sanctioned 'हाँ'                          <- stale
    #   heard_you tenure     'हाँ हाँ हाँ हाँ…'             <- staler
    # It also made every later turn end instantly, because get() returned without waiting —
    # so the borrower never saw the clause she was being asked to repeat.
    stale = 0
    while not answers.empty():
        answers.get_nowait()
        stale += 1
    if stale:
        logger.info("discarded %d utterance(s) spoken before the %s question",
                    stale, clause.id)

    await say_narrowband(session, pool, question, call_id=fsm.call_id)

    # Tell her it is her turn. Until now the only cue that the agent had stopped talking
    # and started waiting was the silence itself, and a borrower who does not know she is
    # being waited for says nothing — which grades as `not_mentioned` and blocks consent on
    # a clause she actually understood. The page turns this into a mic and a countdown.
    events.emit(fsm.call_id, "listening", clause_id=clause.id, timeout_s=timeout,
                # What she has to convey, in the form the agent just spoke it. Every clause
                # carries exactly one value now, so this is one short phrase, not a list to
                # memorise. The digits are already on screen; this is the spoken form of
                # the same fact, so it leaks nothing the page was not already showing.
                expect=[{"raw_text": kv.raw_text, "spoken_text": kv.spoken_text}
                        for kv in clause.key_values])
    try:
        transcript = await asyncio.wait_for(answers.get(), timeout=timeout)
    except TimeoutError:
        transcript = ""
    finally:
        events.emit(fsm.call_id, "listening_done", clause_id=clause.id)

    # Show her what was heard BEFORE grading, because grading can take an LLM round trip
    # and a second of blank screen after she has spoken reads as "it did not hear me".
    # This is her own words echoed back — recognition, not reading — and the verdict that
    # follows is carried by colour, not by text.
    events.emit(fsm.call_id, "heard_you", clause_id=clause.id, transcript=transcript)

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
        for attempt in range(max_attempts):
            if attempt:
                # Restarting a clause with no word of explanation sounds like a fault. She
                # interrupted deliberately; tell her what is about to happen, and that it
                # starts again rather than resuming. NOT interruptible — a second barge-in
                # landing on this line would consume the last attempt and drop the clause.
                await say_narrowband(session, pool,
                                     "माफ़ कीजिए। मैं यह बात शुरू से दोबारा पढ़ता हूँ।",
                                     allow_interruptions=False, call_id=fsm.call_id)
            await deliver_clause(session, fsm, clause, pool, ledger)
            if fsm.state(clause.id).value != "HEARD":
                continue  # PARTIALLY_HEARD -> re-read from the START, never resume
            await run_teach_back(session, fsm, clause,
                                 f"{clause.title_hi} के बारे में आपने क्या समझा?", answers,
                                 pool=pool)
            if fsm.state(clause.id).value == "UNDERSTOOD":
                break

    finished_at = time.monotonic()
    await say_narrowband(session, pool, "क्या आप इन शर्तों पर सहमत हैं?",
                         call_id=fsm.call_id)

    try:
        utterance = await asyncio.wait_for(answers.get(), timeout=60.0)
    except TimeoutError:
        return None

    return evaluate(fsm, utterance, latency_s=time.monotonic() - finished_at)


def wire_touch_barge_in(room, session: AgentSession) -> None:
    """The borrower page's stop button, which until now went nowhere.

    web/call.html publishes {"t":"barge_in"} on the reliable data channel when she taps
    stop. Nothing in this process was listening, so touch barge-in did nothing at all —
    which mattered the moment voice barge-in was switched off, because then she had no way
    to interrupt whatsoever.

    Touch is the interruption path we can actually rely on: it does not care about room
    acoustics, PA volume, or whether echo cancellation held up. It is also the only one
    that works while SAMJHA_VOICE_BARGE_IN=0.
    """
    import contextlib as _ctx

    def _on_data(packet) -> None:
        with _ctx.suppress(Exception):
            if json.loads(bytes(packet.data).decode()).get("t") == "barge_in":
                logger.info("touch barge-in from the borrower")
                session.interrupt()

    room.on("data_received", _on_data)


def wire_transcripts(session: AgentSession) -> asyncio.Queue[str]:
    """Final borrower transcripts, in order. Interim results are ignored on purpose."""
    q: asyncio.Queue[str] = asyncio.Queue()

    @session.on("user_input_transcribed")
    def _on(ev) -> None:
        if ev.is_final and ev.transcript.strip():
            q.put_nowait(ev.transcript.strip())

    return q


_VAD = None


def build_vad():
    """Silero VAD, loaded once per process.

    TURN_HANDLING asks for `"mode": "vad"`, and that option is inert unless a VAD is
    actually handed to AgentSession — there is no error and no warning, interruption
    detection simply never runs. The symptom is the agent reading a clause straight through
    a borrower who is talking over it, which is precisely the behaviour this product exists
    to make impossible: barge-in is how a clause becomes PARTIALLY_HEARD.

    Loaded lazily and cached because it costs ~0.5 s and a call should not pay it twice.
    """
    global _VAD
    if _VAD is None:
        from livekit.plugins import silero

        _VAD = silero.VAD.load()
    return _VAD


def build_session(*, llm=None) -> AgentSession:
    """The session, with the four dangerous defaults set before anything is built on them."""
    return AgentSession(
        stt=build_stt(),
        vad=build_vad(),  # without this, TURN_HANDLING's "mode": "vad" does nothing at all
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

    # "mode": "vad" is inert without a VAD instance, and fails silently when it is missing.
    assert i["mode"] != "vad" or build_vad() is not None, \
        "interruption mode is 'vad' but build_vad() gave nothing — barge-in would not fire"
    assert build_vad() is build_vad(), "VAD must be cached, not reloaded per call"

    print(f"  {len(frames)} frames of {FRAME_MS} ms from 1.000 s of mu-law")
    print(f"  interruption options: {i}")
    print("\nsession audio path + dangerous defaults OK")


if __name__ == "__main__":
    _demo()
