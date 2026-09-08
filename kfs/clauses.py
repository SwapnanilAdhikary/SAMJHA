"""The contract every other module codes against. Do not change signatures casually —
the agent, the eval harness and the web UI all depend on these shapes.

The central idea: a clause is delivered as an ORDERED LIST OF SEGMENTS, each of which is
one Rime flush unit carrying AT MOST ONE comprehension-critical value.

That single constraint does three jobs at once:
  1. intelligibility — one number per sentence is the delivery rule;
  2. heard-through   — "did she hear the APR?" becomes "did segment k finish playing?",
                       which is exact arithmetic over byte counts, not an estimate;
  3. language-independence — it needs no vendor word timestamps, which Rime does not emit
                       for Hindi anyway (measured: see evals/results/battery/FINDINGS.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

# The six comprehension-critical categories the eval measures.
ValueKind = str  # one of:
VALUE_KINDS = (
    "rupee_amount",
    "percentage_apr",
    "tenure_months",
    "emi_amount",
    "account_identifier",
    "date_deadline",
)


class ClauseState(str, Enum):
    """UNDERSTOOD is the ONLY state that permits consent.

    PARTIALLY_HEARD is a trap door: it cannot go directly to UNDERSTOOD. The clause must be
    re-delivered from the START (never resumed from the cut point) and teach-back must pass.
    """

    UNHEARD = "UNHEARD"
    PARTIALLY_HEARD = "PARTIALLY_HEARD"
    HEARD = "HEARD"
    TEACH_BACK_FAIL = "TEACH_BACK_FAIL"
    UNDERSTOOD = "UNDERSTOOD"


@dataclass
class KeyValue:
    """A comprehension-critical value. `value` is TYPED — never a string.

    Scoring compares parsed values, not text, because Hindi has multiple attested
    spellings of the same number (निन्यानवे/निन्यानबे) and ASR providers differ on whether
    they return digits or words. String equality would mark correct answers wrong.
    """

    kind: ValueKind
    value: Decimal | int | str  # str ONLY for account_identifier (leading zeros matter)
    raw_text: str  # as written in the KFS, e.g. "₹1,04,596"
    spoken_text: str  # after the delivery layer


@dataclass
class Segment:
    """One Rime flush unit. Exactly one text -> one {"operation":"flush"}."""

    text: str  # Devanagari. Romanized input is measurably worse — see FINDINGS.md
    key_value: KeyValue | None = None  # at most one, by construction
    audio: bytes = b""

    @property
    def duration_s(self) -> float:
        """mu-law @ 8 kHz is 1 byte per sample, so this is exact, not estimated."""
        return len(self.audio) / 8000.0

    @property
    def carries_key_value(self) -> bool:
        return self.key_value is not None


@dataclass
class Clause:
    """One KFS clause: the unit of consent."""

    id: str
    title_hi: str
    segments: list[Segment]
    state: ClauseState = ClauseState.UNHEARD
    heard_through_s: float = 0.0  # audio actually PLAYED, from playback_position
    teach_back_attempts: list[str] = field(default_factory=list)

    @property
    def total_duration_s(self) -> float:
        return sum(s.duration_s for s in self.segments)

    @property
    def key_values(self) -> list[KeyValue]:
        return [s.key_value for s in self.segments if s.key_value is not None]

    def segment_end_time(self, idx: int) -> float:
        """Cumulative audio time at which segment `idx` finishes."""
        return sum(s.duration_s for s in self.segments[: idx + 1])

    def heard_key_values(self, played_s: float | None = None) -> list[KeyValue]:
        """Which key values fully played. THE core question of the product.

        A value counts as heard only if the ENTIRE segment carrying it finished. A value
        cut off halfway was not communicated, and treating it as heard is exactly the
        failure this product exists to prevent.

        A segment with NO AUDIO has not been heard either, and that guard is load-bearing
        rather than defensive. An unsynthesized segment has duration 0, so without it the
        arithmetic reads `0.0 <= 0.0` — true — and every value in a clause that never
        played reports as heard. Two real paths reach exactly that state:

          * TTS failed for the clause, so `seg.audio` is still empty. `ConsentFSM.
            delivery_dropped` sets heard_through_s = 0.0 and its docstring promises "heard
            NOTHING, never heard everything"; this is what keeps that promise.
          * the clause has been registered for the panel but not yet spoken, which is how
            the whole KFS is shown greyed out before delivery begins.

        Audio arrives as a prefix — `deliver_clause` synthesizes in order and stops at a
        barge-in — so an unsynthesized segment never sits before a synthesized one and the
        cumulative end times stay honest.
        """
        played = self.heard_through_s if played_s is None else played_s
        out = []
        for i, seg in enumerate(self.segments):
            if seg.key_value is None or not seg.audio:
                continue
            if self.segment_end_time(i) <= played + 1e-9:
                out.append(seg.key_value)
        return out

    def all_key_values_heard(self, played_s: float | None = None) -> bool:
        return len(self.heard_key_values(played_s)) == len(self.key_values)


def demo() -> None:
    """Self-check on the heard-through arithmetic — the logic consent depends on."""
    kv = KeyValue("account_identifier", "000512348899", "000512348899", "शून्य शून्य ...")
    c = Clause(
        id="acct",
        title_hi="खाता संख्या",
        segments=[
            Segment(text="आपका खाता नंबर", audio=b"\x00" * 8000),        # 1.0s, no value
            Segment(text="शून्य शून्य ...", key_value=kv, audio=b"\x00" * 16000),  # 2.0s
            Segment(text="है।", audio=b"\x00" * 4000),                    # 0.5s
        ],
    )
    assert c.total_duration_s == 3.5
    assert c.segment_end_time(1) == 3.0

    # Cut off mid-value: NOT heard. This is the whole point.
    assert c.heard_key_values(played_s=2.5) == []
    assert not c.all_key_values_heard(played_s=2.5)

    # Value segment completed: heard, even though the clause did not finish.
    assert c.heard_key_values(played_s=3.0) == [kv]
    assert c.all_key_values_heard(played_s=3.0)

    print("heard-through arithmetic OK")


if __name__ == "__main__":
    demo()
