"""The consent machinery, proved without a network.

None of these needs a room. That is the point, not a limitation: the four FSM invariants
and the teach-back grading are pure logic, and pure logic that only works when a room is
connected is logic nobody can defend in front of a regulator.

Everything here runs offline. The one exception is the mu-law decoder, which is checked
sample-for-sample against ffmpeg — a local binary, not a service.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from decimal import Decimal
from itertools import combinations

import pytest

from agent.consent_fsm import ConsentFSM, ConsentFSMError, jsonl_sink
from agent.rushed_consent import MIN_DELIBERATION_S, evaluate, is_affirmation, rush_markers
from agent.session import (
    TURN_HANDLING,
    PlayoutLedger,
    deliver_clause,
    frames_from_ulaw,
    ulaw_to_pcm16,
)
from agent.teachback import (
    Fact,
    _scrub_digits,
    digit_sequences,
    facts_for,
    grade,
    grade_offline,
    parse_values,
)
from kfs.clauses import Clause, ClauseState, KeyValue, Segment

# 8000 mu-law bytes = 1.000 s exactly. Every duration below is a byte count.
SEC = 8000


def acct_clause(acct: str = "000512348899") -> Clause:
    """Lead 1.0 s, then a 2.0 s segment carrying the identifier, then 0.5 s of tail."""
    kv = KeyValue("account_identifier", acct, acct, "शून्य शून्य ...")
    return Clause(
        id="acct",
        title_hi="खाता संख्या",
        segments=[
            Segment(text="आपका खाता नंबर", audio=b"\x00" * SEC),
            Segment(text="शून्य ...", key_value=kv, audio=b"\x00" * (2 * SEC)),
            Segment(text="है।", audio=b"\x00" * (SEC // 2)),
        ],
    )


def fsm_for(*clauses: Clause, sink=None) -> ConsentFSM:
    return ConsentFSM(list(clauses), call_id="test", sink=sink)


def reach(state: ClauseState) -> tuple[ConsentFSM, Clause]:
    """Drive a fresh FSM into `state`. Used to test every state exhaustively."""
    c = acct_clause()
    f = fsm_for(c)
    if state is ClauseState.UNHEARD:
        return f, c
    f.begin_delivery("acct")
    if state is ClauseState.PARTIALLY_HEARD:
        f.end_delivery("acct", played_s=2.5, interrupted=True)
        return f, c
    f.end_delivery("acct", played_s=3.5, interrupted=False)
    if state is ClauseState.HEARD:
        return f, c
    f.teach_back("acct", transcript="x", passed=state is ClauseState.UNDERSTOOD)
    return f, c


# ------------------------------------------------------------------ rule 1: consent

class TestOnlyUnderstoodPermitsConsent:
    @pytest.mark.parametrize("state", list(ClauseState))
    def test_single_clause(self, state):
        f, _ = reach(state)
        assert f.state("acct") is state
        assert f.may_consent() is (state is ClauseState.UNDERSTOOD)

    def test_one_bad_clause_blocks_the_whole_call(self):
        a, b = acct_clause(), acct_clause()
        b.id = "second"
        f = fsm_for(a, b)
        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=3.5, interrupted=False)
        f.teach_back("acct", transcript="x", passed=True)

        f.begin_delivery("second")
        f.end_delivery("second", played_s=3.5, interrupted=False)
        f.teach_back("second", transcript="x", passed=False)

        assert not f.may_consent()
        assert f.blocking_clauses() == ["second"]

    def test_blocking_clauses_are_in_read_order(self):
        a, b, c = acct_clause(), acct_clause(), acct_clause()
        b.id, c.id = "b", "c"
        f = fsm_for(a, b, c)
        assert f.blocking_clauses() == ["acct", "b", "c"]


# --------------------------------------------------- rule 2: barge-in -> PARTIALLY_HEARD

class TestBargeIn:
    @pytest.mark.parametrize("played_s", [0.0, 0.5, 1.0, 1.5, 2.9, 2.999])
    def test_cut_before_key_value_segment_completes(self, played_s):
        f, _ = reach(ClauseState.UNHEARD)
        f.begin_delivery("acct")
        assert f.end_delivery("acct", played_s=played_s,
                              interrupted=True) is ClauseState.PARTIALLY_HEARD

    def test_cut_after_key_value_but_before_the_tail_is_heard(self):
        """The tail carries nothing comprehension-critical. Punishing it would be theatre."""
        f, _ = reach(ClauseState.UNHEARD)
        f.begin_delivery("acct")
        assert f.end_delivery("acct", played_s=3.0, interrupted=True) is ClauseState.HEARD

    def test_uses_the_clause_contract_not_its_own_arithmetic(self):
        """heard_key_values() in kfs/clauses.py is the contract; the FSM only reads it."""
        f, c = reach(ClauseState.UNHEARD)
        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=2.5, interrupted=True)
        assert c.heard_key_values() == []
        assert f.transitions[-1].heard_key_values == []
        assert f.transitions[-1].unheard_key_values == ["account_identifier=000512348899"]

        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=3.0, interrupted=True)
        assert c.heard_key_values() == c.key_values
        assert f.transitions[-1].unheard_key_values == []

    def test_clause_with_no_key_values_must_play_in_full(self):
        """Nothing to gate on means the whole clause is the gate — 0 == 0 is not "heard"."""
        c = Clause(id="pre", title_hi="प्रस्तावना",
                   segments=[Segment(text="नमस्ते", audio=b"\x00" * (2 * SEC))])
        f = fsm_for(c)
        f.begin_delivery("pre")
        assert f.end_delivery("pre", played_s=0.5,
                              interrupted=True) is ClauseState.PARTIALLY_HEARD
        f.begin_delivery("pre")
        assert f.end_delivery("pre", played_s=2.0, interrupted=False) is ClauseState.HEARD

    def test_no_playback_event_means_heard_nothing(self):
        """LiveKit drops the chat item when interrupted before the first frame."""
        f, c = reach(ClauseState.UNHEARD)
        f.begin_delivery("acct")
        assert f.delivery_dropped("acct") is ClauseState.PARTIALLY_HEARD
        assert c.heard_through_s == 0.0
        assert f.transitions[-1].reason == "no_playback_event"


# ------------------------------------- rule 3: PARTIALLY_HEARD can never reach UNDERSTOOD

class TestPartiallyHeardIsATrapDoor:
    def test_passing_teach_back_does_not_promote(self):
        f, _ = reach(ClauseState.PARTIALLY_HEARD)
        assert f.teach_back("acct", transcript="शून्य शून्य शून्य पाँच",
                            passed=True) is ClauseState.PARTIALLY_HEARD
        assert not f.may_consent()

    def test_the_refusal_is_recorded_not_silent(self):
        f, _ = reach(ClauseState.PARTIALLY_HEARD)
        f.teach_back("acct", transcript="हाँ", passed=True)
        t = f.transitions[-1]
        assert t.reason == "teach_back_refused_state_not_heard"
        assert t.detail["refused_in"] == "PARTIALLY_HEARD"
        assert t.detail["transcript"] == "हाँ"

    def test_repeated_attempts_never_promote(self):
        f, _ = reach(ClauseState.PARTIALLY_HEARD)
        for _ in range(5):
            f.teach_back("acct", transcript="हाँ हाँ", passed=True)
        assert f.state("acct") is ClauseState.PARTIALLY_HEARD

    @pytest.mark.parametrize("state", [ClauseState.UNHEARD, ClauseState.PARTIALLY_HEARD,
                                       ClauseState.TEACH_BACK_FAIL])
    def test_only_heard_accepts_a_teach_back(self, state):
        f, _ = reach(state)
        assert f.teach_back("acct", transcript="x", passed=True) is state

    def test_the_only_route_to_understood_is_heard(self):
        """No sequence of teach-backs reaches UNDERSTOOD without a full delivery first."""
        f, _ = reach(ClauseState.PARTIALLY_HEARD)
        f.teach_back("acct", transcript="x", passed=True)
        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=2.0, interrupted=True)  # cut short again
        f.teach_back("acct", transcript="x", passed=True)
        assert f.state("acct") is not ClauseState.UNDERSTOOD

        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=3.5, interrupted=False)
        assert f.teach_back("acct", transcript="x", passed=True) is ClauseState.UNDERSTOOD

    def test_no_transition_goes_directly_partially_heard_to_understood(self):
        """The property, asserted over the whole log rather than one path."""
        f, _ = reach(ClauseState.PARTIALLY_HEARD)
        for passed in (True, False, True):
            f.teach_back("acct", transcript="x", passed=passed)
        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=3.5, interrupted=False)
        f.teach_back("acct", transcript="x", passed=True)
        assert not any(t.from_state == "PARTIALLY_HEARD" and t.to_state == "UNDERSTOOD"
                       for t in f.transitions)


# -------------------------------------------- rule 4: re-delivery restarts from the start

class TestRedeliveryRestarts:
    def test_two_half_hearings_do_not_add_up(self):
        """2.5 s + 1.0 s must never make 3.5 s. This is the whole reason for the rule."""
        f, c = reach(ClauseState.UNHEARD)
        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=2.5, interrupted=True)
        f.begin_delivery("acct")
        assert c.heard_through_s == 0.0
        assert f.end_delivery("acct", played_s=1.0,
                              interrupted=True) is ClauseState.PARTIALLY_HEARD
        assert c.heard_through_s == 1.0

    def test_restart_is_logged_with_the_state_it_came_from(self):
        f, _ = reach(ClauseState.PARTIALLY_HEARD)
        f.begin_delivery("acct")
        t = f.transitions[-1]
        assert t.reason == "redelivery_from_start"
        assert t.to_state == "UNHEARD"
        assert t.detail["restarted_from"] == "PARTIALLY_HEARD"
        assert t.played_s == 0.0

    def test_a_settled_clause_cannot_be_reopened(self):
        f, _ = reach(ClauseState.UNDERSTOOD)
        with pytest.raises(ConsentFSMError):
            f.begin_delivery("acct")

    def test_teach_back_fail_re_delivers(self):
        f, _ = reach(ClauseState.TEACH_BACK_FAIL)
        f.begin_delivery("acct")
        assert f.end_delivery("acct", played_s=3.5, interrupted=False) is ClauseState.HEARD


# ------------------------------------------------------------------- the consent record

class TestTransitionLog:
    def test_chain_is_unbroken(self):
        """Every transition starts where the previous one ended. A gap is a lost event."""
        f, _ = reach(ClauseState.PARTIALLY_HEARD)
        f.teach_back("acct", transcript="बस करो", passed=True)  # refused, self-loop
        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=3.5, interrupted=False)
        f.teach_back("acct", transcript="x", passed=False)
        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=3.0, interrupted=True)
        f.teach_back("acct", transcript="x", passed=True)

        prev = "UNHEARD"
        for t in f.transitions:
            assert t.from_state == prev, f"gap before {t.reason}"
            prev = t.to_state

    def test_every_row_carries_what_a_dispute_would_need(self):
        f, _ = reach(ClauseState.PARTIALLY_HEARD)
        t = f.transitions[-1]
        assert t.call_id == "test" and t.clause_id == "acct"
        assert t.at.endswith("+00:00") and "T" in t.at  # ISO 8601 UTC
        assert t.monotonic_s >= 0.0
        assert t.played_s == 2.5 and t.clause_duration_s == 3.5
        assert t.reason and t.from_state and t.to_state
        assert t.unheard_key_values == ["account_identifier=000512348899"]

    def test_teach_back_attempts_are_kept_verbatim(self):
        f, c = reach(ClauseState.HEARD)
        f.teach_back("acct", transcript="पहली कोशिश", passed=False)
        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=3.5, interrupted=False)
        f.teach_back("acct", transcript="दूसरी कोशिश", passed=True)
        assert c.teach_back_attempts == ["पहली कोशिश", "दूसरी कोशिश"]
        assert f.transitions[-1].detail["attempt"] == 2

    def test_jsonl_sink_is_append_only_and_valid(self, tmp_path):
        f = ConsentFSM([acct_clause()], call_id="c1", sink=jsonl_sink("c1", tmp_path))
        f.begin_delivery("acct")
        f.end_delivery("acct", played_s=2.0, interrupted=True)
        evaluate(f, "हाँ ठीक है", latency_s=0.2)

        lines = (tmp_path / "c1.jsonl").read_text(encoding="utf-8").splitlines()
        events = [json.loads(x) for x in lines]
        assert [e["kind"] for e in events] == [
            "call_started", "transition", "transition", "consent_decision"]
        assert events[-1]["granted"] is False
        assert events == f.log  # the file and the in-memory record cannot diverge

    def test_unknown_clause_is_an_error_not_a_silent_noop(self):
        f, _ = reach(ClauseState.UNHEARD)
        with pytest.raises(ConsentFSMError):
            f.begin_delivery("nope")

    def test_duplicate_clause_ids_are_rejected(self):
        with pytest.raises(ValueError):
            fsm_for(acct_clause(), acct_clause())

    def test_empty_flow_is_rejected(self):
        with pytest.raises(ValueError):
            ConsentFSM([], call_id="x")


# ----------------------------------------------------------------------- teach-back

class TestNumberParsing:
    @pytest.mark.parametrize("text,expected", [
        ("साढ़े तीन लाख", 350000),                       # colloquial +0.5, applied before लाख
        ("ढाई लाख", 250000),
        ("डेढ़ लाख", 150000),
        ("पौने दो लाख", 175000),
        ("सवा लाख", 125000),
        ("एक लाख चार हज़ार पाँच सौ छियानवे", 104596),
        ("तीन करोड़", 30000000),
        ("अठारह दशमलव पांच", Decimal("18.5")),          # Deepgram-style spelled out
        ("अठारह point पांच", Decimal("18.5")),          # AI4Bharat substitutes English 'point'
        ("18.5", Decimal("18.5")),                       # Sarvam mode=transcribe
        ("₹1,04,596", 104596),                           # Indian grouping is not a separator
        ("१२५०००", 125000),                              # Devanagari digits; NFKC will NOT fold
    ])
    def test_values(self, text, expected):
        assert Decimal(expected) in parse_values(text)

    @pytest.mark.parametrize("a,b", [
        ("निन्यानवे", "निन्यानबे"),  # both attested spellings of 99
        ("अट्ठारह", "अठारह"),        # both attested spellings of 18
        ("पाँच", "पांच"),             # chandrabindu vs anusvara
    ])
    def test_spelling_variants_parse_to_the_same_value(self, a, b):
        assert parse_values(a) == parse_values(b) != []

    def test_a_bare_colloquial_prefix_is_not_a_number(self):
        assert parse_values("साढ़े") == []

    def test_zero_is_a_value(self):
        assert parse_values("शून्य") == [Decimal(0)]

    def test_code_switched_sentence(self):
        got = parse_values("मेरा EMI चार हज़ार तीन सौ बयासी रुपये है")
        assert Decimal(4382) in got


class TestDigitSequences:
    def test_leading_zeros_survive(self):
        """The day-1 pilot watched the raw arm turn 000512348899 into 95058000000."""
        assert "000512348899" in digit_sequences(
            "शून्य शून्य शून्य पाँच एक दो तीन चार आठ आठ नौ नौ")

    def test_latin_zero_inside_devanagari(self):
        """Measured: Sarvam verbatim emits 'नौ आठ चार zero नौ' — mixed scripts in one number."""
        assert digit_sequences("नौ आठ चार zero नौ पांच zero") == ["9840950"]

    def test_literal_digit_run(self):
        assert "9157114007" in digit_sequences("मेरा नंबर 9157114007 है")

    def test_counting_words_are_not_an_identifier(self):
        assert digit_sequences("एक लाख रुपये") == []


class TestOfflineGrader:
    @pytest.fixture
    def facts(self):
        return [
            Fact("f.amount", "rupee_amount", Decimal(350000), "₹3,50,000", "..."),
            Fact("f.tenure", "tenure_months", 36, "36 महीने", "..."),
            Fact("f.acct", "account_identifier", "000512348899", "000512348899", "..."),
        ]

    def test_all_understood_passes(self, facts):
        r = grade_offline(facts, "साढ़े तीन लाख, तीन साल, "
                                 "शून्य शून्य शून्य पाँच एक दो तीन चार आठ आठ नौ नौ")
        assert r.passed and r.graded_by == "offline"
        assert {g.verdict for g in r.grades} == {"understood"}

    def test_tenure_in_years_is_accepted(self, facts):
        r = grade_offline([facts[1]], "तीन साल")
        assert r.passed

    def test_wrong_value_is_wrong_not_missing(self, facts):
        r = grade_offline([facts[0]], "चार लाख")
        assert r.grades[0].verdict == "wrong"

    def test_silence_is_not_mentioned(self, facts):
        r = grade_offline([facts[0]], "मुझे समझ नहीं आया")
        assert r.grades[0].verdict == "not_mentioned"

    def test_dropped_leading_zeros_do_not_pass(self, facts):
        """This is the exact raw-arm failure mode. It must not score as understood."""
        r = grade_offline([facts[2]], "पाँच एक दो तीन चार आठ आठ नौ नौ")
        assert not r.passed

    def test_partial_answer_grades_per_fact(self, facts):
        r = grade_offline(facts, "साढ़े तीन लाख")
        verdicts = {g.fact_id: g.verdict for g in r.grades}
        assert verdicts["f.amount"] == "understood"
        assert verdicts["f.tenure"] == "wrong"  # a number was said, just not this one
        assert not r.passed

    def test_empty_grade_list_never_passes(self):
        assert not grade_offline([], "हाँ").passed


class TestGraderPlumbing:
    def test_falls_back_offline_with_no_key(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        r = grade(acct_clause(), "शून्य शून्य शून्य पाँच एक दो तीन चार आठ आठ नौ नौ")
        assert r.graded_by == "offline" and r.passed
        assert "no OPENROUTER_API_KEY" in r.note

    def test_llm_failure_falls_back_and_says_so(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
        monkeypatch.setattr("agent.teachback.httpx.post", boom)
        r = grade(acct_clause(), "शून्य शून्य शून्य पाँच एक दो तीन चार आठ आठ नौ नौ")
        assert r.graded_by == "offline" and "network down" in r.note

    def test_a_malformed_verdict_never_becomes_a_pass(self, monkeypatch):
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": json.dumps(
                    {"grades": [{"fact_id": "acct#0:account_identifier",
                                 "verdict": "definitely", "reason": "it is 000512348899"}]})}}]}

        monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
        monkeypatch.setattr("agent.teachback.httpx.post", lambda *a, **k: FakeResponse())
        r = grade(acct_clause(), "हाँ")
        assert r.graded_by == "llm" and not r.passed
        assert r.grades[0].verdict == "not_mentioned"

    def test_the_grader_can_never_emit_a_number(self, monkeypatch):
        """The record must not contain a digit the model made up."""
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": json.dumps(
                    {"grades": [{"fact_id": "acct#0:account_identifier",
                                 "verdict": "understood",
                                 "reason": "said 000512348899 and १२३ correctly"}]})}}]}

        monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
        monkeypatch.setattr("agent.teachback.httpx.post", lambda *a, **k: FakeResponse())
        r = grade(acct_clause(), "हाँ")
        assert not any(ch.isdigit() for ch in r.grades[0].reason)

    def test_scrub_handles_devanagari_digits_too(self):
        assert not any(ch.isdigit() for ch in _scrub_digits("१२३ and 456"))

    def test_fact_ids_are_unique_and_stable(self):
        c = acct_clause()
        c.segments.append(Segment(
            text="x", key_value=KeyValue("emi_amount", Decimal(4382), "₹4,382", "..."),
            audio=b"\x00" * SEC))
        ids = [f.id for f in facts_for(c)]
        assert len(set(ids)) == len(ids)
        assert ids == [f.id for f in facts_for(c)]


# --------------------------------------------------------------------- rushed consent

class TestRushedConsent:
    def test_stress_b_is_refused_on_every_available_ground(self):
        """हाँ हाँ ठीक है, बस करो — two seconds in, mid-clause."""
        f, _ = reach(ClauseState.PARTIALLY_HEARD)
        d = evaluate(f, "हाँ हाँ ठीक है, बस करो", latency_s=-1.2)
        assert not d.granted
        assert d.human_callback
        assert "clauses_not_understood:acct" in d.reasons
        assert "arrived_before_clause_finished" in d.reasons
        assert "बस करो" in d.flags and any(x.startswith("repeated:") for x in d.flags)

    def test_the_refusal_is_written_into_the_record(self):
        f, _ = reach(ClauseState.PARTIALLY_HEARD)
        evaluate(f, "हाँ बस करो", latency_s=0.1)
        rec = [e for e in f.log if e["kind"] == "consent_decision"]
        assert len(rec) == 1
        assert rec[0]["granted"] is False
        assert rec[0]["clause_states"] == {"acct": "PARTIALLY_HEARD"}
        assert rec[0]["min_deliberation_s"] == MIN_DELIBERATION_S

    def test_understood_but_still_a_reflex_is_refused(self):
        f, _ = reach(ClauseState.UNDERSTOOD)
        d = evaluate(f, "हाँ ठीक है", latency_s=MIN_DELIBERATION_S - 0.01)
        assert not d.granted and d.human_callback
        assert d.reasons[0].startswith("faster_than_deliberation_floor")

    def test_considered_consent_is_granted(self):
        f, _ = reach(ClauseState.UNDERSTOOD)
        d = evaluate(f, "जी हाँ, मैं सहमत हूँ", latency_s=3.0)
        assert d.granted and not d.human_callback and d.reasons == []

    def test_a_refusal_is_not_a_blocked_consent(self):
        """"नहीं" is not consent, so it does not earn a human callback."""
        f, _ = reach(ClauseState.UNDERSTOOD)
        d = evaluate(f, "नहीं, मुझे नहीं चाहिए", latency_s=5.0)
        assert not d.granted and not d.human_callback
        assert "not_an_affirmation" in d.reasons

    @pytest.mark.parametrize("text,affirm", [
        ("हाँ", True), ("जी हाँ", True), ("ठीक है", True), ("ok", True),
        ("बिल्कुल सही है", True),
        ("नहीं", False), ("नहीं, हाँ नहीं", False), ("रुको", False), ("मुझे समझ नहीं आया", False),
    ])
    def test_affirmation_detection(self, text, affirm):
        assert is_affirmation(text) is affirm

    def test_impatience_alone_does_not_refuse(self):
        """A borrower who understood and is in a hurry has still consented."""
        f, _ = reach(ClauseState.UNDERSTOOD)
        d = evaluate(f, "हाँ ठीक है, जल्दी करो", latency_s=4.0)
        assert d.granted and "जल्दी" in d.flags

    def test_rush_markers_catch_the_doubled_yes(self):
        assert any(m.startswith("repeated:") for m in rush_markers("हाँ हाँ"))


# ------------------------------------------------------------------------ audio path

class TestAudio:
    def test_ulaw_decoder_matches_ffmpeg_exactly(self, tmp_path):
        """audioop is gone in 3.13, so this decoder is hand-rolled — and therefore checked."""
        wav = tmp_path / "ul.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=0.5:sample_rate=8000",
                        "-c:a", "pcm_mulaw", "-ar", "8000", "-ac", "1", str(wav)], check=True)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-f", "mulaw", "-"],
                             capture_output=True, check=True).stdout
        ref = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-f", "s16le",
                              "-acodec", "pcm_s16le", "-ar", "8000", "-ac", "1", "-"],
                             capture_output=True, check=True).stdout
        assert ulaw_to_pcm16(raw) == ref

    def test_frame_slicing_is_lossless(self):
        payload = bytes(range(256)) * 40  # 10240 bytes = 1.28 s
        frames = list(frames_from_ulaw(payload))
        assert sum(f.samples_per_channel for f in frames) == len(payload)
        assert {f.sample_rate for f in frames} == {8000}

    def test_no_playback_event_means_heard_nothing(self):
        assert PlayoutLedger().take() == (0.0, True)

    def test_the_four_dangerous_defaults_are_overridden(self):
        i = TURN_HANDLING["interruption"]
        assert i["resume_false_interruption"] is False
        assert i["backchannel_boundary"] == (0, 0)
        assert i["enabled"] is True
        assert TURN_HANDLING["preemptive_generation"]["enabled"] is False


# -------------------------------------------------------------- the loop, against fakes

class FakeHandle:
    def __init__(self, interrupted: bool) -> None:
        self.interrupted = interrupted

    async def wait_for_playout(self) -> None:
        return None


class FakeSession:
    """Plays a script of (seconds_played, interrupted) — one entry per say() call."""

    def __init__(self, ledger: PlayoutLedger, script: list[tuple[float, bool]]) -> None:
        self.ledger, self.script, self.said = ledger, list(script), []

    def say(self, text, *, audio=None, allow_interruptions=True):
        self.said.append(text)
        played, interrupted = self.script.pop(0) if self.script else (0.0, True)
        if played or not interrupted:
            self.ledger.events.append(
                type("Ev", (), {"playback_position": played, "interrupted": interrupted})())
        return FakeHandle(interrupted)


class FakePool:
    def __init__(self) -> None:
        self.stops = 0
        self.requested: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.requested.append(text)
        return b"\x00" * SEC

    async def hard_stop(self) -> None:
        self.stops += 1


class TestClauseLoop:
    def test_full_delivery_is_heard(self):
        c, ledger, pool = acct_clause(), PlayoutLedger(), FakePool()
        f = fsm_for(c)
        s = FakeSession(ledger, [(1.0, False), (2.0, False), (0.5, False)])
        out = asyncio.run(deliver_clause(s, f, c, pool, ledger))
        assert out.state == "HEARD" and out.played_s == 3.5 and pool.stops == 0
        assert len(s.said) == 3

    def test_barge_in_mid_number_stops_the_socket_and_asks_for_nothing_more(self):
        """Stress A: the segments after the cut are never requested, so nothing is stale."""
        c, ledger, pool = acct_clause(), PlayoutLedger(), FakePool()
        c.segments[1].audio = b""  # force a synthesize call so we can count requests
        f = fsm_for(c)
        s = FakeSession(ledger, [(1.0, False), (0.8, True)])
        out = asyncio.run(deliver_clause(s, f, c, pool, ledger))

        assert out.state == "PARTIALLY_HEARD" and out.interrupted
        assert pool.stops == 1
        assert len(s.said) == 2  # the tail segment was never spoken
        assert f.state("acct") is ClauseState.PARTIALLY_HEARD

    def test_a_dropped_first_segment_is_heard_nothing(self):
        c, ledger, pool = acct_clause(), PlayoutLedger(), FakePool()
        f = fsm_for(c)
        s = FakeSession(ledger, [(0.0, True)])  # interrupted before any frame -> no event
        out = asyncio.run(deliver_clause(s, f, c, pool, ledger))
        assert out.played_s == 0.0 and out.state == "PARTIALLY_HEARD"

    def test_redelivery_through_the_loop_starts_over(self):
        c, ledger, pool = acct_clause(), PlayoutLedger(), FakePool()
        f = fsm_for(c)
        s = FakeSession(ledger, [(1.0, False), (0.5, True),
                                 (1.0, False), (2.0, False), (0.5, False)])
        assert asyncio.run(deliver_clause(s, f, c, pool, ledger)).state == "PARTIALLY_HEARD"
        assert asyncio.run(deliver_clause(s, f, c, pool, ledger)).state == "HEARD"
        assert s.said[2] == c.segments[0].text  # started at segment 0, not at the cut point


def test_real_kfs_clauses_run_through_the_loop():
    """Integration: the actual fixture -> kfs.build_clauses -> FSM path, still no network.

    Guarded with importorskip because kfs/ is another workstream's directory; a skip here
    means that pipeline is not ready yet, not that the agent is broken.
    """
    pytest.importorskip("kfs.build_clauses")
    from agent.main import _fixture_clauses

    # load_clauses(kfs) is now pure — a call's loan comes from its own document, fetched
    # per job, so one worker can serve two borrowers without a process-global env var
    # deciding which loan they both hear. _fixture_clauses is the offline dev path.
    clauses, _ = _fixture_clauses()
    assert clauses, "no clauses built from the synthetic fixture"

    # The contract the consent mechanic rests on: one key value per flush segment, so the
    # count of values equals the count of segments carrying one.
    for c in clauses:
        assert len(c.key_values) == sum(1 for s in c.segments if s.carries_key_value)

    # NOTHING HAS BEEN SYNTHESIZED, so nothing can be reported as heard. Without the
    # no-audio guard in Clause.heard_key_values the arithmetic reads 0.0 <= 0.0 and every
    # value in every clause claims to have been heard before a word was spoken.
    for c in clauses:
        assert c.heard_key_values() == []
        assert not c.all_key_values_heard()

    # Nothing has played, so nothing is understood, whatever the borrower then says.
    f = ConsentFSM(clauses, call_id="integration")
    for c in clauses:
        f.begin_delivery(c.id)
        f.delivery_dropped(c.id)
    assert f.blocking_clauses() == [c.id for c in clauses]

    # delivery_dropped promises "heard NOTHING, never heard everything". Assert it of the
    # transition rows that reach the consent record, not just of the clause states: this
    # is the TTS-failure path scripts/talk.py takes, and it really happened in a live run.
    dropped = [t for t in f.log if t.get("reason") == "no_playback_event"]
    assert dropped, "expected a no_playback_event transition per clause"
    for t in dropped:
        assert t["heard_key_values"] == [], t
        assert t["played_s"] == 0.0

    d = evaluate(f, "हाँ हाँ ठीक है, बस करो", latency_s=-2.0)
    assert not d.granted and d.human_callback


def test_no_two_clause_states_are_confusable():
    """A guard on the LOCKED enum: distinct values, and UNDERSTOOD alone permits consent."""
    assert all(a.value != b.value for a, b in combinations(ClauseState, 2))
    assert sum(1 for s in ClauseState if s is ClauseState.UNDERSTOOD) == 1
