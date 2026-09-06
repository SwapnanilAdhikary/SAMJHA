"""Parser regression for the eval scorer. No network.

Every case here is a way the scorer could silently produce a wrong number rather than an
error — which is worse than a crash, because a wrong number gets published. Most are taken
verbatim from real transcripts in evals/results/battery/.

    uv run pytest tests/test_evals.py -q
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from evals.asr_score import agreement, digit_strings, parse_numbers, recovered

ROOT = Path(__file__).resolve().parents[1]


def nums(text: str) -> set[Decimal]:
    return set(parse_numbers(text))


# ------------------------------------------------------------------ digits and scripts
def test_devanagari_digits_map_to_ascii():
    """NFKC does NOT fold U+0966-U+096F. An explicit map is the only way."""
    assert Decimal(104596) in nums("कुल राशि १०४५९६ रुपये")
    assert digit_strings("०००५१२") == ["000512"]


def test_indian_grouping_and_ascii_decimals():
    assert Decimal(104596) in nums("कुल राशि 1,04,596 रुपये है")
    assert Decimal("18.5") in nums("वार्षिक दर 18.5% है")


# ------------------------------------------------------------------ Indic multipliers
@pytest.mark.parametrize("text,value", [
    ("एक लाख चार हज़ार पाँच सौ छियानवे रुपये", 104596),
    ("एक लाख पच्चीस हज़ार", 125000),
    ("तीन करोड़ चौबीस लाख पचास हज़ार", 32450000),
    ("दो अरब", 2000000000),
    ("दो हज़ार छब्बीस", 2026),
])
def test_multipliers(text, value):
    assert Decimal(value) in nums(text)


def test_nukta_variants_are_the_same_word():
    """हज़ार and हजार differ by U+093C only. So do करोड़ and करोड."""
    assert nums("चार हज़ार") == nums("चार हजार")
    assert nums("दो करोड़") == nums("दो करोड")


# ------------------------------------------------------------------ orthographic variants
@pytest.mark.parametrize("a,b", [
    ("निन्यानवे", "निन्यानबे"),   # व ~ ब — the delivery layer emits one, NeMo's gold the other
    ("अट्ठारह", "अठारह"),        # geminate cluster written both ways
    ("पाँच", "पांच"),            # chandrabindu ~ anusvara
    ("पैंतालीस", "पैतालीस"),      # anusvara dropped entirely
    ("तिरेसठ", "तिरसठ"),         # ए matra — the delivery layer says one, Sarvam returns the other
    ("छः", "छह"),                # indic-numtowords says छः; every ASR here says छह
    ("बाईस", "बाइस"),            # long vowel ई ~ इ
    ("अट्ठाईस", "अट्ठाइस"),        # gemination AND long vowel in one word
    ("इकतीस", "इकत्तीस"),         # doubled consonant
])
def test_attested_spellings_are_one_value(a, b):
    assert nums(a) == nums(b) != set()


def test_decimal_written_as_punctuation():
    """Deepgram renders the spoken दशमलव as a full stop: `अठारह. पांच प्रतिशत` is a real
    transcript of 18.5. A bare '.' only joins a number already in progress."""
    assert Decimal("18.5") in nums("सालाना प्रतिशत दर यानी APR अठारह. पांच प्रतिशत है.")
    # ...and puts a space after it when the digits pass is on: `13. 97` is one number.
    assert nums("ब्याज की दर 13. 97 प्रतिशत है.") == {Decimal("13.97")}
    assert nums("अवधि छह महीने है. पांच सौ") == {Decimal(6), Decimal(500)}


def test_day_of_month_ordinal():
    """Coda reads `1 मई` as `पहली मई` — correct Hindi, and a lost date if unhandled."""
    item = {"type": "date_deadline", "value": "2026-05-01"}
    assert recovered(item, "अगली किश्त पहली मई दो हज़ार छब्बीस को देय है.")


def test_month_spelling_variants():
    item = {"type": "date_deadline", "value": "2026-10-03"}
    assert recovered(item, "परिपक्वता तीन अक्तूबर दो हज़ार छब्बीस है.")   # अक्तूबर ~ अक्टूबर
    assert recovered(item, "परिपक्वता तीन October दो हज़ार छब्बीस है.")   # Deepgram code-switches
    assert not recovered(item, "परिपक्वता तीन नवंबर दो हज़ार छब्बीस है.")  # wrong month is wrong


# ------------------------------------------------------------------ decimals
def test_dashamlav_and_english_point_both_accepted():
    """AI4Bharat's production text.py substitutes the English word "point" for '.', so a
    scorer that only knows दशमलव marks a correct answer wrong."""
    assert Decimal("18.5") in nums("अठारह दशमलव पांच प्रतिशत")
    assert Decimal("18.5") in nums("अठारह point पांच प्रतिशत")
    assert Decimal("99.99") in nums("निन्यानबे दशमलव नौ नौ")
    assert Decimal("17.07") in nums("सत्रह दशमलव शून्य सात प्रतिशत")


def test_colloquial_forms():
    """First-class constants in NeMo's Hindi grammar, and what a borrower actually says."""
    assert Decimal("1.5") in nums("डेढ़ साल")
    assert Decimal("2.5") in nums("ढाई प्रतिशत")
    assert Decimal(350000) in nums("साढ़े तीन लाख रुपए")
    assert Decimal("18.5") in nums("यानी साढ़े अठारह प्रतिशत")
    assert Decimal("2.25") in nums("सवा दो")
    assert Decimal("3.75") in nums("पौने चार")


def test_consecutive_units_are_not_summed():
    """Real Hindi never says "पाँच सात" for 57. Summing a spoken digit run would invent a
    value nobody said, and it would do so silently."""
    assert nums("नौ एक पाँच सात") == {Decimal(9), Decimal(1), Decimal(5), Decimal(7)}


# ------------------------------------------------------------------ digit sequences
def test_latin_zero_inside_a_devanagari_run():
    """Sarvam verbatim really does mix scripts inside one number. A Devanagari-only map
    drops every zero in an account number — the exact property the claim rests on."""
    assert digit_strings("शून्य zero शून्य पांच एक दो") == ["000512"]
    assert digit_strings("संख्या जीरो जीरो पाँच") == ["005"]


def test_leading_zeros_survive_and_matter():
    item = {"type": "account_identifier", "value": "000512348899"}
    assert recovered(item, "आपके लोन खाते की संख्या 000512348899 है।")
    assert recovered(item, "शून्य शून्य शून्य पाँच एक दो तीन चार आठ आठ नौ नौ")
    # Same digits, leading zeros lost: a different account, and must score as a miss.
    assert not recovered(item, "आपके लोन खाते की संख्या 512348899 है।")


def test_quantity_reading_is_not_a_recovered_sequence():
    """The day-1 raw failure. Deepgram heard the account number as an Indian-scale quantity;
    a borrower hearing "nine billion fifteen crore" cannot verify anything."""
    item = {"type": "account_identifier", "value": "9157114007"}
    assert not recovered(item, "आपका खाता number नौ अरब पंद्रह करोड़ इकहत्तर लाख चौदह हज़ार सात है.")
    assert recovered(item, "आपका खाता number नौ एक पांच सात एक एक चार शून्य शून्य सात है.")
    # ASR punctuates digit runs unpredictably; adjacent numeric tokens are one run.
    assert recovered(item, "आपका खाता नंबर 9,15,71,14,007 है।")
    assert recovered(item, "आपका खाता नंबर 9157, 1,14,007 है।")


# ------------------------------------------------------------------ typed scoring
@pytest.mark.parametrize("kind,value,text,hit", [
    ("rupee_amount", 104596, "कुल राशि एक लाख चार हज़ार पाँच सौ छियानवे रुपये है", True),
    ("rupee_amount", 104596, "कुल राशि एक लाख चार हज़ार पाँच सौ छियानवे है", True),
    ("rupee_amount", 104596, "कुल राशि एक लाख चार हज़ार पाँच सौ पचानवे है", False),
    ("percentage_apr", "18.5", "वार्षिक ब्याज दर 18.5 प्रतिशत है।", True),
    ("percentage_apr", "18.5", "वार्षिक ब्याज दर अठारह प्रतिशत है।", False),
    ("tenure_months", 36, "अवधि छत्तीस महीने यानी तीन साल है.", True),
    ("emi_amount", 4382, "किश्त चार हज़ार तीन सौ बयासी रुपए है", True),
    ("date_deadline", "2026-03-15", "तारीख़ पंद्रह मार्च दो हज़ार छब्बीस है", True),
    ("date_deadline", "2026-03-15", "तारीख़ 15 मार्च 2026 है", True),
    ("date_deadline", "2026-03-15", "तारीख़ पंद्रह April दो हज़ार छब्बीस है", False),
    ("date_deadline", "2026-03-15", "तारीख़ 15/03/2026 है", True),
])
def test_recovered_by_type(kind, value, text, hit):
    assert recovered({"type": kind, "value": value}, text) is hit


def test_agreement_reports_both_scorers():
    rows = [
        {"item_id": "a", "arm": "baseline", "scorer": "sarvam", "recovered": True},
        {"item_id": "a", "arm": "baseline", "scorer": "deepgram", "recovered": True},
        {"item_id": "b", "arm": "baseline", "scorer": "sarvam", "recovered": True},
        {"item_id": "b", "arm": "baseline", "scorer": "deepgram", "recovered": False},
    ]
    ag = agreement(rows)
    assert ag["n"] == 2 and ag["observed_agreement"] == 0.5
    assert ag["sarvam_recovery"] == 1.0 and ag["deepgram_recovery"] == 0.5


# ------------------------------------------------------------------ the corpus itself
def corpus() -> list[dict]:
    return json.loads((ROOT / "evals/corpus/utterances.json").read_text(encoding="utf-8"))


def test_corpus_shape():
    items = corpus()
    assert len(items) == 120
    counts = {k: sum(1 for i in items if i["type"] == k) for i in items for k in [i["type"]]}
    assert set(counts.values()) == {20}, counts
    assert len(counts) == 6


def test_account_identifiers_include_leading_zeros():
    """The headline finding. If the corpus has no leading-zero cases it cannot test it."""
    accts = [i["value"] for i in corpus() if i["type"] == "account_identifier"]
    assert sum(a.startswith("0") for a in accts) >= 8, accts


def test_carriers_are_varied():
    """One template used 120 times would measure one sentence, not a category."""
    assert len({i["carrier_template"] for i in corpus()}) >= 50


def test_both_arm_texts_self_score():
    """A perfect transcript of either arm must recover the value. If it does not, the
    corpus and the scorer disagree and every measured number is meaningless."""
    for item in corpus():
        for arm in ("carrier_a", "carrier_b"):
            assert recovered(item, item[arm]), (item["id"], arm, item[arm])


class TestClauseBoundaryAccumulation:
    """Regression: a clause boundary must settle the accumulator.

    Found by cross-checking the agent workstream's teach-back parser against this one —
    both were written independently and both had it. "साढ़े तीन लाख, तीन साल" accumulated
    across the comma into 350003: a value nobody said, plausible enough to survive review,
    and silent. It would have corrupted scored results rather than failing loudly.

    Two separate invisibilities caused it: a standalone comma matched no alternative in
    _TOKEN, and danda (U+0964) sits outside the Devanagari ranges the word alternative
    covers, so Hindi sentence ends were dropped as well.
    """

    def test_comma_breaks_accumulation(self):
        assert [int(x) for x in parse_numbers("साढ़े तीन लाख, तीन साल")] == [350000, 3]

    def test_fraction_does_not_cross_comma(self):
        got = parse_numbers("अठारह दशमलव पांच, तीन साल")
        assert [float(x) for x in got] == [18.5, 3.0]

    def test_danda_separates_amounts(self):
        assert [int(x) for x in parse_numbers("डेढ़ लाख। ढाई लाख।")] == [150000, 250000]

    def test_comma_inside_a_number_still_groups(self):
        """The numeric alternative must keep winning over the boundary alternative."""
        assert [int(x) for x in parse_numbers("खाता नंबर 9,157,114,007 है।")] == [9157114007]

    def test_deepgram_split_decimal_still_rejoins(self):
        """Deepgram writes a spoken decimal point as '. ' — two tokens, one number."""
        assert [float(x) for x in parse_numbers("13. 97")] == [13.97]
