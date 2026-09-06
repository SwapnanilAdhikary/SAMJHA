"""A scripted call, replayed as real events.

Why this exists: the state machine is the demo. If LiveKit, the agent or the network is
not up — and on the night, one of them will not be — there still has to be something to
record. So DEMO MODE is not a mock of the UI: it pushes the same events through the same
ingest path into the same tables, and the consent record it produces is built and hashed
by exactly the code that serves a live call. The only thing that is synthetic is the
timing of the events, and the record says so (`mode: "demo"`).

NO FABRICATED MEASUREMENTS. The script carries no latency figures, no VER, no WER. It is
a rehearsal of the state machine, and every number that could be mistaken for a
measurement is left out on purpose.

The arc, which is the demo script for the talk:
  1-3  normal delivery, with one teach-back FAILURE and a re-read (clauses go UNDERSTOOD)
  4    barge-in 0.7 s before the account-number segment finishes -> PARTIALLY_HEARD,
       consent blocked, clause re-read FROM THE START, then UNDERSTOOD
  5    rushed consent two seconds in -> REFUSED, flagged for human callback

Spoken text comes from `delivery.normalizer.hindi`, not from hand-typed strings, so what
the panel shows is genuinely what the delivery layer would send.

Self-check:  uv run python -m api.demo
"""

from __future__ import annotations

from delivery.normalizer.hindi import digits, percent, rupees, tenure_months

KFS_REF = "SYNTH-KFS-004 (synthetic; no real borrower data)"
LABEL = "डेमो कॉल — व्यक्तिगत ऋण"


def _kv(kind: str, raw: str, value, spoken: str, end_s: float) -> dict:
    return {"kind": kind, "raw_text": raw, "value": str(value),
            "spoken_text": spoken, "segment_end_s": end_s}


# Durations are what a Rime 8 kHz mu-law read of these clauses runs to, rounded. They are
# the fixture's timeline, not a measurement of anything.
CLAUSES = [
    {
        "clause_id": "sanctioned_amount",
        "title_hi": "स्वीकृत ऋण राशि",
        "title_en": "Sanctioned loan amount",
        "total_duration_s": 7.0,
        "key_values": [_kv("rupee_amount", "₹1,25,000", 125000, rupees("₹1,25,000"), 4.2)],
    },
    {
        "clause_id": "apr",
        "title_hi": "वार्षिक प्रतिशत दर (APR)",
        "title_en": "Annual Percentage Rate",
        "total_duration_s": 9.5,
        "key_values": [_kv("percentage_apr", "18.5%", 18.5, percent("18.5"), 5.5)],
    },
    {
        "clause_id": "emi_tenure",
        "title_hi": "मासिक किस्त और अवधि",
        "title_en": "EMI and tenure",
        "total_duration_s": 11.0,
        "key_values": [
            _kv("emi_amount", "₹4,382", 4382, rupees("₹4,382"), 4.8),
            _kv("tenure_months", "36 महीने", 36, tenure_months(36), 8.6),
        ],
    },
    {
        "clause_id": "loan_account",
        "title_hi": "ऋण खाता संख्या",
        "title_en": "Loan account number",
        "total_duration_s": 12.4,
        # The category the day-1 pilot found Rime reads as an Indian-scale QUANTITY when
        # sent raw (exact recovery 1/5 vs 5/5 through the delivery layer), with the
        # leading zeros lost. It is the primary claim's payload, so it is the clause the
        # demo interrupts.
        "key_values": [_kv("account_identifier", "000512348899", "000512348899",
                           digits("000512348899"), 9.1)],
    },
    {
        "clause_id": "cooling_off",
        "title_hi": "कूलिंग-ऑफ अवधि",
        "title_en": "Cooling-off period",
        "total_duration_s": 6.0,
        "key_values": [_kv("tenure_months", "3 दिन", 3, "तीन दिन", 3.4)],
    },
]

_BY_ID = {c["clause_id"]: c for c in CLAUSES}


def _state(clause_id: str, state: str, heard_s: float, *, prev: str | None = None,
           active: bool = False, reason: str = "") -> dict:
    c = _BY_ID[clause_id]
    return {
        "type": "clause_state",
        "clause_id": clause_id,
        "title_hi": c["title_hi"],
        "title_en": c["title_en"],
        "prev_state": prev,
        "state": state,
        "active": active,
        "heard_through_s": heard_s,
        "total_duration_s": c["total_duration_s"],
        "reason": reason,
        "key_values": [
            kv | {"heard": kv["segment_end_s"] <= heard_s + 1e-9} for kv in c["key_values"]
        ],
    }


def _registered(c: dict, ordinal: int) -> dict:
    return {"type": "clause_registered", "ordinal": ordinal, "state": "UNHEARD",
            "active": False, "heard_through_s": 0.0,
            "clause_id": c["clause_id"], "title_hi": c["title_hi"],
            "title_en": c["title_en"], "total_duration_s": c["total_duration_s"],
            "key_values": [kv | {"heard": False} for kv in c["key_values"]]}


def _deliver(clause_id: str, from_state: str, to_state: str, *, steps: int = 3,
             pace: float = 0.85) -> list[tuple[float, dict]]:
    """Animate the heard-through bar filling, then land the state change.

    `active` is a display flag, not an FSM state — see api.events.
    """
    total = _BY_ID[clause_id]["total_duration_s"]
    out = [(0.9, _state(clause_id, from_state, 0.0, prev=from_state, active=True,
                        reason="delivering"))]
    for i in range(1, steps + 1):
        out.append((pace, _state(clause_id, from_state, round(total * i / steps, 2),
                                 prev=from_state, active=True, reason="delivering")))
    out.append((0.5, _state(clause_id, to_state, total, prev=from_state,
                            reason="clause delivered in full")))
    return out


def script() -> list[tuple[float, dict]]:
    """(seconds to wait before this event, event). ~50 s end to end."""
    s: list[tuple[float, dict]] = [
        (0.0, {"type": "call_start", "label": LABEL, "kfs_ref": KFS_REF}),
    ]
    for i, c in enumerate(CLAUSES):
        s.append((0.15, _registered(c, i)))

    s += _deliver("sanctioned_amount", "UNHEARD", "HEARD")
    s += [
        (1.3, {"type": "teach_back", "clause_id": "sanctioned_amount", "attempt": 1,
               "transcript": "मुझे एक लाख पच्चीस हज़ार रुपए मिलेंगे",
               "grade": "pass — amount restated correctly", "passed": True}),
        (0.7, _state("sanctioned_amount", "UNDERSTOOD", 7.0, prev="HEARD",
                     reason="teach-back passed")),
    ]

    s += _deliver("apr", "UNHEARD", "HEARD")
    s += [
        (1.3, {"type": "teach_back", "clause_id": "apr", "attempt": 1,
               "transcript": "ब्याज चौदह प्रतिशत है",
               "grade": "fail — restated the interest rate (14%), not the APR (18.5%)",
               "passed": False}),
        (0.6, _state("apr", "TEACH_BACK_FAIL", 9.5, prev="HEARD",
                     reason="teach-back failed: interest rate given instead of APR")),
        (2.2, {"type": "note", "text":
               "APR ≠ ब्याज दर. Re-reading the clause FROM THE START — never resumed "
               "from the cut point."}),
        (0.8, _state("apr", "HEARD", 9.5, prev="TEACH_BACK_FAIL",
                     reason="re-delivered from the start")),
        (1.4, {"type": "teach_back", "clause_id": "apr", "attempt": 2,
               "transcript": "साढ़े अट्ठारह प्रतिशत, सारे खर्च मिलाकर",
               "grade": "pass — APR restated with the colloquial half-form",
               "passed": True}),
        (0.7, _state("apr", "UNDERSTOOD", 9.5, prev="HEARD", reason="teach-back passed")),
    ]

    s += _deliver("emi_tenure", "UNHEARD", "HEARD")
    s += [
        (1.3, {"type": "teach_back", "clause_id": "emi_tenure", "attempt": 1,
               "transcript": "हर महीने चार हज़ार तीन सौ बयासी रुपए, तीन साल तक",
               "grade": "pass — both values restated", "passed": True}),
        (0.7, _state("emi_tenure", "UNDERSTOOD", 11.0, prev="HEARD",
                     reason="teach-back passed")),
    ]

    # ---- Stress A: barge-in mid-number. The money shot. --------------------------
    s += [
        (1.0, _state("loan_account", "UNHEARD", 0.0, prev="UNHEARD", active=True,
                     reason="delivering")),
        (0.9, _state("loan_account", "UNHEARD", 3.0, prev="UNHEARD", active=True,
                     reason="delivering")),
        (0.9, _state("loan_account", "UNHEARD", 6.2, prev="UNHEARD", active=True,
                     reason="delivering")),
        (0.9, _state("loan_account", "UNHEARD", 8.4, prev="UNHEARD", active=True,
                     reason="delivering — mid account number")),
        (0.5, {"type": "note", "text":
               "बीच में टोका — borrower spoke over the account number. Playout stopped; "
               "socket closed and reopened (Rime /ws3 has no cancel primitive: `clear` "
               "does not drop in-flight synthesis)."}),
        (0.4, _state("loan_account", "PARTIALLY_HEARD", 8.4, prev="UNHEARD",
                     reason="barge-in 0.7 s before the account-number segment finished — "
                            "8.40 s of 12.40 s played out")),
        (2.6, {"type": "note", "text":
               "Consent BLOCKED. PARTIALLY_HEARD cannot go to UNDERSTOOD: the segment "
               "carrying the value did not finish playing, so the value was not "
               "communicated."}),
        (2.4, _state("loan_account", "PARTIALLY_HEARD", 0.0, prev="PARTIALLY_HEARD",
                     active=True, reason="re-delivering FROM THE START")),
        (0.9, _state("loan_account", "PARTIALLY_HEARD", 4.5, prev="PARTIALLY_HEARD",
                     active=True, reason="re-delivering FROM THE START")),
        (0.9, _state("loan_account", "PARTIALLY_HEARD", 9.1, prev="PARTIALLY_HEARD",
                     active=True, reason="re-delivering FROM THE START")),
        # The reason string is the audit trail. The intermediate re-delivery frames are
        # PARTIALLY_HEARD -> PARTIALLY_HEARD and write no transition row (correctly — they
        # are not state changes), so the fact that the clause restarted from zero has to
        # be carried on the transition that does get written.
        (0.9, _state("loan_account", "HEARD", 12.4, prev="PARTIALLY_HEARD",
                     reason="re-delivered FROM THE START and completed — never resumed "
                            "from the cut point")),
        (1.4, {"type": "teach_back", "clause_id": "loan_account", "attempt": 1,
               "transcript": "शून्य शून्य शून्य पाँच एक दो तीन चार आठ आठ नौ नौ",
               "grade": "pass — all 12 digits including the three leading zeros",
               "passed": True}),
        (0.7, _state("loan_account", "UNDERSTOOD", 12.4, prev="HEARD",
                     reason="teach-back passed")),
    ]

    # ---- Stress B: rushed consent. --------------------------------------------
    s += [
        (1.0, _state("cooling_off", "UNHEARD", 0.0, prev="UNHEARD", active=True,
                     reason="delivering")),
        (0.8, _state("cooling_off", "UNHEARD", 1.2, prev="UNHEARD", active=True,
                     reason="delivering")),
        (0.8, _state("cooling_off", "UNHEARD", 2.1, prev="UNHEARD", active=True,
                     reason="delivering")),
        (0.5, {"type": "note", "text":
               "Borrower: «हाँ हाँ ठीक है, बस करो» at 2.1 s of 6.0 s."}),
        (0.4, _state("cooling_off", "PARTIALLY_HEARD", 2.1, prev="UNHEARD",
                     reason="rushed consent — interrupted before the cooling-off period "
                            "was stated")),
        (1.4, {"type": "consent", "clause_id": "cooling_off", "decision": "REFUSED",
               "reason": "Consent offered 2.1 s into a 6.0 s clause. The cooling-off "
                         "period was never played out. Blanket agreement is not informed "
                         "consent.",
               "flagged_for_callback": True, "blocking_clauses": ["cooling_off"]}),
        (1.2, {"type": "consent", "clause_id": None, "decision": "REFUSED",
               "reason": "Consent for the Key Facts Statement is refused: 1 of 5 clauses "
                         "is not UNDERSTOOD. Flagged for human callback.",
               "flagged_for_callback": True, "blocking_clauses": ["cooling_off"]}),
        (0.8, {"type": "call_end"}),
    ]
    return s


def duration_s() -> float:
    return sum(d for d, _ in script())


def _demo() -> None:
    """Self-check: the script actually exercises every state and ends in a refusal."""
    from api import events, records, store

    sc = script()
    states = {e["state"] for _, e in sc if e["type"] == "clause_state"}
    assert states == set(events.CLAUSE_STATES), f"missing states: {set(events.CLAUSE_STATES) - states}"

    conn = store.connect(":memory:")
    store.create_call(conn, "d", mode="demo")
    for _, ev in sc:
        store.apply_event(conn, "d", events.normalize(ev))

    rec = records.build_record(conn, "d")
    by_id = {c["clause_id"]: c for c in rec["clauses"]}
    assert len(rec["clauses"]) == 5
    assert by_id["loan_account"]["final_state"] == "UNDERSTOOD"
    assert by_id["cooling_off"]["final_state"] == "PARTIALLY_HEARD"
    assert by_id["cooling_off"]["key_values_not_heard"], "the cooling-off value must be unheard"
    assert rec["consent"]["decision"] == "REFUSED"
    assert len(rec["refusals"]) == 2
    assert rec["consent_permitted"] is False

    # The barge-in has to be visible in the audit trail, not just in the final state.
    reasons = [t["reason"] for t in by_id["loan_account"]["transitions"]]
    assert any("barge-in" in r for r in reasons), reasons
    assert any("FROM THE START" in r for r in reasons), reasons

    print(f"demo script OK — {len(sc)} events, {duration_s():.1f}s, "
          f"consent {rec['consent']['decision']}, sha256 {records.record_hash(rec)[:16]}…")


if __name__ == "__main__":
    _demo()
