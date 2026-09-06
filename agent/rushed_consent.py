"""Stress B — the rushed-consent gate. Refusing is the feature.

*"हाँ हाँ ठीक है, बस करो"* two seconds in. Every consent product on the market takes that
and issues the loan. This one refuses it, says why, flags a human callback, and writes the
refusal into the consent record with the same durability as a grant.

Three independent grounds for refusal, checked in order of how obviously wrong they are:

  1. **State.** A clause that is not UNDERSTOOD blocks consent, full stop. This is the FSM's
     rule, not a heuristic, and it cannot be overridden by anything the borrower says.
  2. **Arrival before the clause finished.** A "हाँ" that lands while the agent is still
     reading is an interruption, not agreement. LiveKit's default
     `backchannel_boundary=(1.0, 1.0)` would suppress exactly this utterance as a
     backchannel — which is why agent/session.py sets it to (0, 0) and we classify here.
  3. **Reflex speed.** An affirmation arriving faster than a person can weigh a loan term
     is a reflex. `MIN_DELIBERATION_S` is a product judgement, stated as one, not measured.

Rush markers ("बस करो", "जल्दी", a doubled हाँ) are recorded as a FLAG, never as a refusal
on their own. A borrower who genuinely understood and is simply impatient has still
consented, and inventing a fourth refusal ground we cannot defend would be dishonest.

Self-check:  uv run python -m agent.rushed_consent
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from agent.consent_fsm import ConsentFSM

# A considered "yes" to a loan term takes longer than a reflex "yes". This number is a
# product judgement, not a measurement, and is stated as such in the record.
MIN_DELIBERATION_S = 1.5

_AFFIRM = (
    "हाँ", "हां", "हा", "जी", "जी हाँ", "ठीक है", "ठीक", "सही है", "मंज़ूर", "मंजूर",
    "राज़ी", "राजी", "स्वीकार", "बिल्कुल", "ओके", "ok", "okay", "yes", "yeah", "haan",
    "theek hai", "agree", "agreed", "i agree",
)

_REFUSE = ("नहीं", "ना", "मत", "रुको", "no", "not", "wait", "stop")

# Impatience, not disagreement. Flagged, never fatal on its own.
_RUSH_MARKERS = (
    "बस करो", "बस", "जल्दी", "छोड़ो", "आगे बढ़ो", "चलो", "जो भी", "कर दो",
    "just do it", "hurry", "skip", "whatever", "fast",
)


@dataclass
class ConsentDecision:
    """The consent record's verdict row. Serialised verbatim into events/{call_id}.jsonl."""

    granted: bool
    utterance: str
    latency_s: float
    reasons: list[str]
    flags: list[str]
    human_callback: bool
    blocking_clauses: list[str]
    clause_states: dict[str, str]
    min_deliberation_s: float = MIN_DELIBERATION_S
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="milliseconds"))


def _norm(text: str) -> str:
    return re.sub(r"[।.,!?;:]+", " ", text.strip().lower())


def is_affirmation(text: str) -> bool:
    """Does this utterance read as agreement? Explicit refusal wins over any yes in it.

    "हाँ लेकिन नहीं" is not consent, and a substring check that ignored word order would
    call it consent.
    """
    t = f" {_norm(text)} "
    if any(f" {w} " in t or t.strip().startswith(w) for w in _REFUSE):
        return False
    return any(w in t for w in _AFFIRM)


def rush_markers(text: str) -> list[str]:
    t = _norm(text)
    found = [m for m in _RUSH_MARKERS if m in t]
    # A doubled affirmation ("हाँ हाँ") is the classic tell and no single keyword catches it.
    for w in ("हाँ", "हां", "ठीक है", "yes", "ok"):
        if len(re.findall(re.escape(w), t)) >= 2:
            found.append(f"repeated:{w}")
    return found


def evaluate(fsm: ConsentFSM, utterance: str, *, latency_s: float,
             record: bool = True) -> ConsentDecision:
    """Decide on a consent utterance and (by default) write the decision into the record.

    `latency_s` is seconds from the end of the last clause's playout to the start of this
    utterance. NEGATIVE means the borrower spoke while the agent was still reading — that
    is an interruption, and it is treated as one.
    """
    reasons: list[str] = []
    flags = rush_markers(utterance)
    blocking = fsm.blocking_clauses()

    affirmed = is_affirmation(utterance)
    if not affirmed:
        reasons.append("not_an_affirmation")
    if blocking:
        reasons.append(f"clauses_not_understood:{','.join(blocking)}")
    if latency_s < 0:
        reasons.append("arrived_before_clause_finished")
    elif latency_s < MIN_DELIBERATION_S:
        reasons.append(f"faster_than_deliberation_floor:{latency_s:.2f}s")

    granted = not reasons
    decision = ConsentDecision(
        granted=granted,
        utterance=utterance,
        latency_s=round(latency_s, 3),
        reasons=reasons,
        flags=flags,
        # An utterance that was never agreement is not a refused consent — it is just not
        # consent. Only a blocked *attempt* to consent earns a human callback.
        human_callback=not granted and affirmed,
        blocking_clauses=blocking,
        clause_states=fsm.states(),
    )
    if record:
        fsm.record_decision(asdict(decision))
    return decision


def _demo() -> None:
    """Stress B, offline: the rushed consent is refused and the refusal is in the record."""
    from decimal import Decimal

    from kfs.clauses import Clause, ClauseState, KeyValue, Segment

    kv = KeyValue("percentage_apr", Decimal("18.5"), "18.5%", "अठारह दशमलव पाँच प्रतिशत")
    clause = Clause(id="apr", title_hi="वार्षिक प्रतिशत दर",
                    segments=[Segment(text="...", key_value=kv, audio=b"\x00" * 24000)])
    fsm = ConsentFSM([clause], call_id="stress_b")

    fsm.begin_delivery("apr")
    fsm.end_delivery("apr", played_s=2.0, interrupted=True)  # cut off mid-value

    rushed = evaluate(fsm, "हाँ हाँ ठीक है, बस करो", latency_s=-1.0)
    assert not rushed.granted
    assert rushed.human_callback
    assert "clauses_not_understood:apr" in rushed.reasons
    assert "arrived_before_clause_finished" in rushed.reasons
    assert any(f.startswith("repeated:") for f in rushed.flags)

    # Same words, everything understood, but still a reflex: refused on speed alone.
    fsm.begin_delivery("apr")
    fsm.end_delivery("apr", played_s=3.0, interrupted=False)
    fsm.teach_back("apr", transcript="अठारह दशमलव पाँच प्रतिशत", passed=True)
    assert fsm.state("apr") is ClauseState.UNDERSTOOD

    reflex = evaluate(fsm, "हाँ ठीक है", latency_s=0.4)
    assert not reflex.granted and reflex.human_callback

    considered = evaluate(fsm, "जी हाँ, मैं सहमत हूँ", latency_s=3.2)
    assert considered.granted and not considered.human_callback

    assert not is_affirmation("नहीं, हाँ नहीं")

    written = [e for e in fsm.log if e["kind"] == "consent_decision"]
    assert len(written) == 3, written
    for d in written:
        print(f"  granted={str(d['granted']):<5} callback={str(d['human_callback']):<5} "
              f"{d['reasons']}")
    print("\nrushed-consent gate OK — refusals are in the record")


if __name__ == "__main__":
    _demo()
