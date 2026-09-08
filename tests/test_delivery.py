"""The /ws3 frame protocol, and the per-segment attribution the consent mechanic needs.

These run against a fake socket that speaks Rime's ACTUAL protocol, established live and
recorded in `evals/results/battery/frames.json`: `chunk` frames carrying base64 audio in
JSON, and ONE `done` per flush. There is no `flush_done` and no `segment_done` event —
the committed transcript contains 1543 `chunk`, 15 `done`, 1 `timestamps`, and nothing
else.

The bug these exist to prevent, which shipped and was caught by taking a real call:
`_synthesize_once` used to send every text and every flush up front, then `eos`, and
advance its segment index on a `flush_done` frame that does not exist. So every chunk
landed in segment 0 — the first segment held the entire clause and the rest held zero
bytes.

That is not an accounting detail. `Clause.heard_key_values` asks "did the segment carrying
this value finish playing?", and a zero-length segment can never satisfy it. A clause read
perfectly to the borrower would record its number as NOT heard and block consent for good.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from delivery import rime_ws3
from kfs.clauses import Clause, KeyValue, Segment


class FakeRime:
    """Rime /ws3 as it actually behaves: chunks, then one `done`, per flush.

    `bytes_per_flush` is indexed by flush order so a test can give each segment a
    distinctive length and prove the attribution is not just non-empty but correct.
    """

    def __init__(self, bytes_per_flush: list[int], *, chunks_per_flush: int = 3,
                 emit_done: bool = True) -> None:
        self.bytes_per_flush = bytes_per_flush
        self.chunks_per_flush = chunks_per_flush
        self.emit_done = emit_done
        self.sent: list[dict] = []
        self.outbox: list[str] = []
        self.flushes = 0
        # How much was still unread when each flush arrived. A caller that reads its
        # segment's audio before asking for the next one leaves this at 0 every time; a
        # caller that pipelines every flush first leaves a growing backlog. This is the
        # only observable difference between the correct and the broken implementation,
        # since both send the same frames in the same order.
        self.unread_at_flush: list[int] = []

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("operation") != "flush":
            return

        # Answer THIS flush, and only this one — which is the behaviour that makes
        # per-segment attribution possible.
        self.unread_at_flush.append(len(self.outbox))
        total = self.bytes_per_flush[self.flushes] if self.flushes < len(self.bytes_per_flush) else 0
        self.flushes += 1
        per = max(1, total // self.chunks_per_flush)
        remaining = total
        while remaining > 0:
            n = min(per, remaining)
            remaining -= n
            self.outbox.append(json.dumps(
                {"type": "chunk", "data": base64.b64encode(b"\x7f" * n).decode()}))
        if self.emit_done:
            self.outbox.append(json.dumps({"type": "done", "contextId": None}))

    async def recv(self) -> str:
        if not self.outbox:
            # Nothing more for this flush. A real socket would simply go quiet, which is
            # what the per-recv timeout is for.
            raise TimeoutError
        return self.outbox.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run(coro):
    """The repo has no async tests and pytest-asyncio is not a dependency."""
    return asyncio.run(coro)


@pytest.fixture
def fake_connect(monkeypatch):
    """Swap websockets.connect for a FakeRime the test configures.

    A dummy RIME_API_KEY is set because `_synthesize_once` builds the Authorization header
    before it ever reaches the (faked) socket, and `api_key()` raises SystemExit without
    one. Set here rather than read from the environment so these tests are hermetic and
    pass identically on a machine with no credentials.
    """
    monkeypatch.setenv("RIME_API_KEY", "test-key-never-sent-anywhere")
    holder: dict = {}

    def connect(*_a, **_kw):
        return holder["ws"]

    monkeypatch.setattr(rime_ws3.websockets, "connect", connect)

    def install(ws):
        holder["ws"] = ws
        return ws

    return install


class TestPerSegmentAttribution:
    def test_each_flush_segment_gets_its_own_audio(self, fake_connect):
        """THE regression test. Distinct lengths, so a mis-attribution cannot pass."""
        fake_connect(FakeRime([8000, 16000, 4000]))

        result = _run(rime_ws3.synthesize(
            ["पहला वाक्य।", "वह दर है, इकतीस प्रतिशत।", "तीसरा वाक्य।"],
            key_value_flags=[False, True, False]))

        assert [len(s.audio) for s in result.segments] == [8000, 16000, 4000]
        # mu-law at 8 kHz is 1 byte per sample, so these are exact, not rounded.
        assert [s.duration_s for s in result.segments] == [1.0, 2.0, 0.5]
        assert result.duration_s == 3.5

    def test_no_segment_is_left_empty(self, fake_connect):
        """The shape of the old bug: segment 0 holding everything, the rest holding none."""
        fake_connect(FakeRime([8000, 8000, 8000, 8000]))
        result = _run(rime_ws3.synthesize(["a", "b", "c", "d"]))

        assert all(len(s.audio) > 0 for s in result.segments), \
            "a flush segment with no audio can never be 'heard', so its value blocks consent"
        assert len({len(s.audio) for s in result.segments}) == 1

    def test_each_flush_is_read_before_the_next_is_sent(self, fake_connect):
        """The property that actually distinguishes the fix from the bug.

        Both implementations send text, flush, text, flush, eos in that order — asserting
        the order alone passes against the broken one too, which is why this asserts the
        INTERLEAVING instead: nothing may still be unread when the next flush goes out.
        Pipelining leaves a backlog, and a backlog is what made every chunk land in
        segment 0.
        """
        ws = fake_connect(FakeRime([1000, 2000, 3000]))
        _run(rime_ws3.synthesize(["one", "two", "three"]))

        assert ws.unread_at_flush == [0, 0, 0], (
            "audio was still unread when the next flush was sent, so chunks cannot be "
            f"attributed to a segment: backlog {ws.unread_at_flush}"
        )
        ops = [m.get("operation") or "text" for m in ws.sent]
        assert ops == ["text", "flush", "text", "flush", "text", "flush", "eos"]
        assert [m["text"] for m in ws.sent if "text" in m] == ["one", "two", "three"]

    def test_a_silent_flush_is_recorded_not_hidden(self, fake_connect):
        """Rime returning nothing for a segment must leave it empty, not borrow audio.

        An empty segment is the honest outcome and the FSM already refuses to credit it
        as heard. Quietly folding it into a neighbour would put a value in the ledger
        that was never spoken.
        """
        fake_connect(FakeRime([8000, 0, 8000]))
        result = _run(rime_ws3.synthesize(["a", "b", "c"]))
        assert [len(s.audio) for s in result.segments] == [8000, 0, 8000]

    def test_it_survives_a_socket_that_never_says_done(self, fake_connect):
        """No `done` at all: the per-recv timeout ends the segment rather than hanging.

        Recorded in `frames` so a debugging session can see what happened.
        """
        fake_connect(FakeRime([8000, 8000], emit_done=False))
        result = _run(rime_ws3.synthesize(["a", "b"], timeout=0.01))

        assert [len(s.audio) for s in result.segments] == [8000, 8000]
        assert any(f.get("type") == "__timeout__" for f in result.frames)


class TestTheSocketPoolSurvivesRime:
    """Rime is US-only with no India region and it fails mid-call.

    A live call read two clauses in Hindi and then crashed: a barge-in called
    `hard_stop()`, which re-opened its warm spare, and Rime answered HTTP 502. The
    exception went up through `deliver_clause` and `run_consent_flow` and killed the job.

    The warm spare exists so the 250-350 ms reconnect lands on the reconnect rather than
    on the borrower. Trading the entire consent call for that optimisation is the wrong
    way round.
    """

    def _pool(self, monkeypatch, *, fail_after: int):
        """A pool whose socket opens succeed `fail_after` times, then 502 forever."""
        from agent.session import RimeSocketPool

        monkeypatch.setenv("RIME_API_KEY", "test-key-never-sent-anywhere")
        opened = {"n": 0}

        class Sock:
            def __init__(self):
                self.closed = False

            async def send(self, _raw):
                pass

            async def close(self):
                self.closed = True

        async def connect_with_retry(**_kw):
            opened["n"] += 1
            if opened["n"] > fail_after:
                raise RuntimeError("Rime /ws3 refused a connection: HTTP 502")
            return Sock()

        monkeypatch.setattr(rime_ws3, "connect_with_retry", connect_with_retry)
        return RimeSocketPool(), opened

    def test_a_barge_in_survives_rime_refusing_the_replacement_spare(self, monkeypatch):
        """The exact live crash: live + spare open, then every further open 502s."""
        pool, opened = self._pool(monkeypatch, fail_after=2)
        _run(pool.start())
        assert pool._live is not None and pool._spare is not None

        # hard_stop promotes the spare (fine) and then fails to open a new one.
        _run(pool.hard_stop())          # must NOT raise

        assert pool._live is not None, "the promoted spare must still be usable"
        assert pool._spare is None, "no spare, so the next barge-in pays the latency"

    def test_hard_stop_never_raises_even_with_no_spare_to_promote(self, monkeypatch):
        """Worst case: nothing can be opened at all. Still no exception."""
        pool, _ = self._pool(monkeypatch, fail_after=1)
        _run(pool.start())
        assert pool._spare is None      # start() already could not get one

        _run(pool.hard_stop())          # must NOT raise
        assert pool._live is None       # honest: we have no socket
        assert pool._spare is None

    def test_start_succeeds_without_a_spare(self, monkeypatch):
        """A call that can speak beats no call. The spare is best-effort at startup too."""
        pool, _ = self._pool(monkeypatch, fail_after=1)
        _run(pool.start())
        assert pool._live is not None and pool._spare is None

    def test_aclose_is_safe_on_a_dead_socket(self, monkeypatch):
        pool, _ = self._pool(monkeypatch, fail_after=2)
        _run(pool.start())

        async def boom():
            raise OSError("already dead")

        pool._live.close = boom
        _run(pool.aclose())             # must NOT raise
        assert pool._live is None and pool._spare is None


class TestAClauseRimeCannotSpeak:
    """A clause we could not synthesize was certainly not heard."""

    def test_a_synthesis_failure_blocks_consent_instead_of_ending_the_call(self):
        from agent.consent_fsm import ConsentFSM
        from agent.session import deliver_clause

        kv = KeyValue("percentage_apr", 18.5, "18.5%", "साढ़े अठारह प्रतिशत")
        clause = Clause(id="apr", title_hi="सालाना कुल दर", segments=[
            Segment(text="अब सालाना दर।"),
            Segment(text="वह दर है, साढ़े अठारह प्रतिशत।", key_value=kv),
        ])
        fsm = ConsentFSM([clause], call_id="c")

        class DeadPool:
            async def synthesize(self, _text, **_kw):
                raise RuntimeError("Rime /ws3 refused a connection: HTTP 502")

            async def hard_stop(self):
                pass

        class Ledger:
            def take(self):
                return (0.0, False)

        outcome = _run(deliver_clause(None, fsm, clause, DeadPool(), Ledger()))

        # Not raised, and the clause is left blocking consent.
        assert outcome.clause_id == "apr"
        assert outcome.state == "PARTIALLY_HEARD"
        assert outcome.played_s == 0.0
        assert fsm.blocking_clauses() == ["apr"]
        # Nothing played, so nothing may be recorded as heard.
        assert clause.heard_key_values() == []


class TestTheConsentMechanicOverRealDurations:
    """Tie the byte counts to the question the product actually asks."""

    def test_a_value_in_a_later_segment_can_be_heard(self, fake_connect):
        """Under the old bug this was impossible: segment 1 had zero length.

        The account number lives in segment 1 of 3. Play the whole clause and it must
        count as heard; cut it 0.1s short of that segment's end and it must not.
        """
        fake_connect(FakeRime([8000, 16000, 4000]))
        result = _run(rime_ws3.synthesize(["intro", "the number", "outro"],
                                          key_value_flags=[False, True, False]))

        kv = KeyValue("account_identifier", "000512348899", "000512348899", "शून्य …")
        clause = Clause(id="acct", title_hi="ऋण खाता संख्या", segments=[
            Segment(text=s.text, key_value=kv if s.carries_key_value else None,
                    audio=bytes(s.audio))
            for s in result.segments
        ])

        assert clause.total_duration_s == 3.5
        assert clause.segment_end_time(1) == 3.0

        # Heard in full.
        assert clause.heard_key_values(played_s=3.0) == [kv]
        assert clause.all_key_values_heard(played_s=3.0)
        # Cut off 0.1s before the segment carrying it finished: NOT heard.
        assert clause.heard_key_values(played_s=2.9) == []
        # And 96% of the clause is still not enough if the cut lands mid-value.
        assert clause.heard_key_values(played_s=3.5 * 0.96) == [kv]  # 3.36s > 3.0s
        assert clause.heard_key_values(played_s=2.99) == []
