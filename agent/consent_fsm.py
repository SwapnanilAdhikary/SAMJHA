"""The consent state machine. This file IS the consent record.

    UNHEARD ──deliver──▶ HEARD ──teach-back pass──▶ UNDERSTOOD
       │                   └──fail──▶ TEACH_BACK_FAIL ──re-deliver──▶ HEARD
       └──barge-in before a key-value segment completes──▶ PARTIALLY_HEARD
                                                          └──re-deliver FROM START──▶ HEARD

Four rules, all load-bearing, all unit-tested in tests/test_agent.py:

  1. UNDERSTOOD is the only state permitting consent.
  2. A barge-in that cuts a key-value segment short leaves the clause PARTIALLY_HEARD.
  3. PARTIALLY_HEARD can NEVER go straight to UNDERSTOOD. Teach-back offered in that state
     is refused and logged, not graded. The clause must be re-delivered FROM THE START.
  4. Re-delivery never resumes from the cut point, so the heard-through ledger is REPLACED
     on each delivery, never accumulated across deliveries. Accumulating would let two
     half-hearings add up to a "heard" value the borrower never heard in one piece.

Two things this deliberately does not do:

  * It does not reimplement heard-through arithmetic. `Clause.heard_key_values()` and
    `all_key_values_heard()` in kfs/clauses.py are the contract; this module only feeds
    them a playout position and reads the answer.
  * It does not decide *why* a delivery ended. LiveKit's `PlaybackFinishedEvent` carries
    `playback_position` — audio measured as actually played out — and that number, not an
    estimate, is what `end_delivery()` takes.

"No event means heard nothing." When a LiveKit speech is interrupted before any frame
plays, `forwarded_text` is "" and the chat item is dropped entirely — there is no event to
observe. `delivery_dropped()` exists so that case is recorded explicitly as zero seconds
played, because the silent-default alternative (assume it finished) is exactly the failure
this product exists to prevent.

Self-check:  uv run python -m agent.consent_fsm
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from kfs.clauses import Clause, ClauseState, KeyValue

# Float seconds compared against float seconds. Playout positions arrive from a different
# clock than our byte-count durations, so an exact >= would reject a segment that finished.
EPS = 1e-6

EVENTS_DIR = Path("events")


@dataclass
class Transition:
    """One row of the consent record. Complete enough to defend or dispute a loan.

    `heard_key_values` is stored as rendered text rather than object ids because this row
    has to be readable years later by someone who does not have this codebase.
    """

    call_id: str
    clause_id: str
    from_state: str
    to_state: str
    reason: str
    at: str  # ISO 8601, UTC
    monotonic_s: float  # elapsed since FSM construction; immune to wall-clock jumps
    played_s: float
    clause_duration_s: float
    heard_key_values: list[str]
    unheard_key_values: list[str]
    detail: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return "transition"


def _kv_label(kv: KeyValue) -> str:
    return f"{kv.kind}={kv.raw_text}"


def jsonl_sink(call_id: str, directory: Path = EVENTS_DIR) -> Callable[[dict], None]:
    """Append-only JSONL, one event per line. The web UI tails it; Pathway can too.

    Opened and closed per line on purpose: a crashed call must leave a readable record,
    and at one line per state transition the cost is irrelevant.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{call_id}.jsonl"

    def write(event: dict) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    return write


class ConsentFSM:
    """Owns the clause states, the transition log and the consent decision.

    One instance per call. `sink` receives every event as a plain dict — pass
    `jsonl_sink(call_id)` for the real thing, or collect them in a list in tests.
    """

    def __init__(
        self,
        clauses: list[Clause],
        *,
        call_id: str,
        sink: Callable[[dict], None] | None = None,
    ) -> None:
        if not clauses:
            raise ValueError("a consent flow with no clauses cannot produce consent")
        ids = [c.id for c in clauses]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate clause ids: {ids}")

        self.call_id = call_id
        self.clauses = {c.id: c for c in clauses}
        self.order = ids
        self.transitions: list[Transition] = []
        self.log: list[dict] = []  # every event, JSON-ready, in order
        self._sink = sink
        self._t0 = time.monotonic()

        self._emit({"kind": "call_started", "call_id": call_id, "clauses": ids,
                    "at": _now(), "monotonic_s": 0.0})

    # ------------------------------------------------------------------ delivery

    def begin_delivery(self, clause_id: str, *, reason: str = "deliver") -> None:
        """Start (or restart) a clause. ALWAYS from the beginning.

        Resetting `heard_through_s` to zero here is rule 4. There is no resume path and
        there is deliberately no argument to request one.
        """
        clause = self._clause(clause_id)
        if clause.state is ClauseState.UNDERSTOOD:
            raise ConsentFSMError(f"{clause_id} is already UNDERSTOOD; re-reading it would "
                                  "reopen a settled clause")
        prior = clause.state
        clause.heard_through_s = 0.0
        self._transition(
            clause,
            to=ClauseState.UNHEARD,
            reason="redelivery_from_start" if prior is not ClauseState.UNHEARD else reason,
            detail={"restarted_from": prior.value},
        )

    def end_delivery(self, clause_id: str, *, played_s: float,
                     interrupted: bool) -> ClauseState:
        """Record what actually played. `played_s` is PlaybackFinishedEvent.playback_position.

        A clause is HEARD when every segment carrying a key value finished playing. A
        barge-in after the last value but before the closing words still counts as HEARD:
        what was cut off carried nothing comprehension-critical, and pretending otherwise
        would make the state machine punish the borrower for the tail of a sentence.

        A clause with no key values at all has nothing to gate on, so it must play in full.
        """
        clause = self._clause(clause_id)
        clause.heard_through_s = max(0.0, played_s)

        if clause.key_values:
            heard = clause.all_key_values_heard()
        else:
            heard = clause.heard_through_s + EPS >= clause.total_duration_s

        to = ClauseState.HEARD if heard else ClauseState.PARTIALLY_HEARD
        reason = (
            "delivered_in_full" if heard and not interrupted
            else "interrupted_after_all_key_values" if heard
            else "interrupted_before_key_value_completed" if interrupted
            else "delivery_ended_short"
        )
        self._transition(clause, to=to, reason=reason,
                         detail={"interrupted": interrupted})
        return clause.state

    def delivery_dropped(self, clause_id: str, *, note: str = "no playback event") -> ClauseState:
        """No playback event arrived. That means heard NOTHING, never heard everything.

        LiveKit drops the chat item entirely when a speech is interrupted before its first
        frame, so the absence of evidence is the whole signal here.
        """
        clause = self._clause(clause_id)
        clause.heard_through_s = 0.0
        self._transition(clause, to=ClauseState.PARTIALLY_HEARD,
                         reason="no_playback_event", detail={"note": note})
        return clause.state

    # ----------------------------------------------------------------- teach-back

    def teach_back(self, clause_id: str, *, transcript: str, passed: bool,
                   grades: list[dict] | None = None) -> ClauseState:
        """Apply a teach-back result. Grading itself lives in agent/teachback.py.

        Refused outright unless the clause is HEARD. That refusal is rule 3: a borrower
        who did not hear the number cannot be credited with understanding it, however
        convincing her answer sounds — she may be repeating it back from the sales call.
        """
        clause = self._clause(clause_id)
        clause.teach_back_attempts.append(transcript)
        detail = {"transcript": transcript, "grades": grades or [],
                  "attempt": len(clause.teach_back_attempts)}

        if clause.state is not ClauseState.HEARD:
            self._transition(
                clause, to=clause.state, reason="teach_back_refused_state_not_heard",
                detail=detail | {"refused_in": clause.state.value},
            )
            return clause.state

        self._transition(
            clause,
            to=ClauseState.UNDERSTOOD if passed else ClauseState.TEACH_BACK_FAIL,
            reason="teach_back_pass" if passed else "teach_back_fail",
            detail=detail,
        )
        return clause.state

    # -------------------------------------------------------------------- consent

    def blocking_clauses(self) -> list[str]:
        """Clause ids that are not UNDERSTOOD, in read order. Empty means consent is open."""
        return [cid for cid in self.order
                if self.clauses[cid].state is not ClauseState.UNDERSTOOD]

    def may_consent(self) -> bool:
        return not self.blocking_clauses()

    def record_decision(self, decision: dict) -> None:
        """Write a consent decision — granted OR refused — into the record.

        A refusal is a product feature and gets the same durability as a grant.
        """
        self._emit({"kind": "consent_decision", "call_id": self.call_id,
                    "at": _now(), "monotonic_s": self._elapsed(), **decision})

    # ---------------------------------------------------------------------- misc

    def state(self, clause_id: str) -> ClauseState:
        return self._clause(clause_id).state

    def states(self) -> dict[str, str]:
        return {cid: self.clauses[cid].state.value for cid in self.order}

    def _clause(self, clause_id: str) -> Clause:
        try:
            return self.clauses[clause_id]
        except KeyError:
            raise ConsentFSMError(f"unknown clause {clause_id!r}") from None

    def _elapsed(self) -> float:
        return round(time.monotonic() - self._t0, 3)

    def _transition(self, clause: Clause, *, to: ClauseState, reason: str,
                    detail: dict) -> None:
        heard = clause.heard_key_values()
        t = Transition(
            call_id=self.call_id,
            clause_id=clause.id,
            from_state=clause.state.value,
            to_state=to.value,
            reason=reason,
            at=_now(),
            monotonic_s=self._elapsed(),
            played_s=round(clause.heard_through_s, 4),
            clause_duration_s=round(clause.total_duration_s, 4),
            heard_key_values=[_kv_label(kv) for kv in heard],
            unheard_key_values=[_kv_label(kv) for kv in clause.key_values
                                if kv not in heard],
            detail=detail,
        )
        clause.state = to
        self.transitions.append(t)
        self._emit({"kind": "transition", **asdict(t)})

    def _emit(self, event: dict) -> None:
        self.log.append(event)
        if self._sink is not None:
            self._sink(event)


class ConsentFSMError(RuntimeError):
    """A caller drove the machine somewhere it must not go."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _demo() -> None:
    """The four rules, end to end, with no network and no LiveKit."""
    from kfs.clauses import Segment

    kv = KeyValue("account_identifier", "000512348899", "000512348899", "शून्य ...")
    clause = Clause(
        id="acct",
        title_hi="खाता संख्या",
        segments=[
            Segment(text="आपका खाता नंबर", audio=b"\x00" * 8000),               # 1.0 s
            Segment(text="शून्य ...", key_value=kv, audio=b"\x00" * 16000),     # 2.0 s
            Segment(text="है।", audio=b"\x00" * 4000),                          # 0.5 s
        ],
    )
    fsm = ConsentFSM([clause], call_id="demo")

    fsm.begin_delivery("acct")
    assert fsm.end_delivery("acct", played_s=2.5, interrupted=True) is ClauseState.PARTIALLY_HEARD

    # Rule 3: a convincing answer in PARTIALLY_HEARD is refused, not graded.
    assert fsm.teach_back("acct", transcript="शून्य शून्य ...", passed=True) is ClauseState.PARTIALLY_HEARD
    assert not fsm.may_consent()

    # Rule 4: re-delivery restarts. 2.5 s heard before + 1.0 s now must NOT sum to heard.
    fsm.begin_delivery("acct")
    assert clause.heard_through_s == 0.0
    assert fsm.end_delivery("acct", played_s=1.0, interrupted=True) is ClauseState.PARTIALLY_HEARD

    fsm.begin_delivery("acct")
    assert fsm.end_delivery("acct", played_s=3.5, interrupted=False) is ClauseState.HEARD
    assert fsm.teach_back("acct", transcript="शून्य शून्य पाँच ...", passed=False) is ClauseState.TEACH_BACK_FAIL
    assert not fsm.may_consent()

    fsm.begin_delivery("acct")
    fsm.end_delivery("acct", played_s=3.5, interrupted=False)
    assert fsm.teach_back("acct", transcript="...", passed=True) is ClauseState.UNDERSTOOD
    assert fsm.may_consent()

    print(f"{len(fsm.transitions)} transitions logged")
    for t in fsm.transitions:
        print(f"  {t.from_state:>16} -> {t.to_state:<16} {t.reason}")
    print("\nconsent FSM OK")


if __name__ == "__main__":
    _demo()
