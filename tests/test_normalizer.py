"""Regression tests for the Hindi verbaliser.

These test the RULES, not the library's orthography. indic-numtowords and NeMo's Hindi
gold data disagree on attested spellings (निन्यानवे/निन्यानबे for 99, अट्ठारह/अठारह for 18)
and both are correct Hindi. Asserting exact strings would make this suite brittle for no
benefit — and, more importantly, that same variance is why the EVAL scores parsed typed
values rather than strings. A string-equality scorer would count correct answers wrong.
"""

from __future__ import annotations

import pytest

from delivery.normalizer.hindi import (
    DECIMAL_POINT,
    PAISE,
    PERCENT,
    RUPEE,
    cardinal,
    decimal_number,
    digits,
    percent,
    rupees,
    tenure_months,
)


class TestIndianGrouping:
    """हज़ार 10^3, लाख 10^5, करोड़ 10^7 — the 2-2-3 grouping, not thousands."""

    @pytest.mark.parametrize("n,contains", [
        (1_000, "हज़ार"),
        (1_00_000, "लाख"),
        (1_00_00_000, "करोड़"),
    ])
    def test_scale_words(self, n, contains):
        assert contains in cardinal(n)

    def test_comma_grouped_input_parses(self):
        """A KFS writes ₹1,25,000. It must parse as 125000, not 125."""
        assert rupees("₹1,25,000") == rupees(125000)

    def test_rejects_out_of_range(self):
        """The ceiling is guarded, not discovered at 2am: the library raises an opaque
        TypeError past ~10^8."""
        with pytest.raises(ValueError):
            cardinal(10**9)


class TestMoneyDecimalRule:
    """The rule that is easy to get wrong: paise is a CARDINAL, not digit-by-digit."""

    def test_whole_rupees_have_no_paise(self):
        assert PAISE not in rupees(80000)
        assert rupees(80000).endswith(RUPEE)

    def test_paise_read_as_cardinal(self):
        out = rupees("₹123.57")
        assert out == f"{cardinal(123)} {RUPEE} {cardinal(57)} {PAISE}"
        # 57 as one word, NOT "five seven"
        assert out.endswith(f"{cardinal(57)} {PAISE}")

    def test_bare_decimal_uses_digit_by_digit(self):
        """Same '.', different rule — switched by the currency symbol."""
        out = decimal_number("99.99")
        assert out == f"{cardinal(99)} {DECIMAL_POINT} {digits('99')}"
        assert DECIMAL_POINT in out

    def test_the_two_decimal_rules_differ(self):
        """The actual regression guard: currency and bare decimals must not converge."""
        assert DECIMAL_POINT in decimal_number("123.57")
        assert DECIMAL_POINT not in rupees("₹123.57")


class TestPercent:
    """The class NO library covers — NeMo's Hindi grammar has no % / प्रतिशत at all."""

    def test_whole_percent(self):
        assert percent(14, confirm=False) == f"{cardinal(14)} {PERCENT}"

    def test_decimal_percent_is_digit_by_digit(self):
        assert percent("18.5", confirm=False) == (
            f"{cardinal(18)} {DECIMAL_POINT} {cardinal(5)} {PERCENT}"
        )

    def test_anchor_and_confirm_on_half(self):
        """साढ़े is how an APR is actually spoken. Say it twice, two ways."""
        assert percent("18.5").endswith(f"यानी साढ़े {cardinal(18)} {PERCENT}")

    def test_no_confirm_when_not_half(self):
        assert "यानी" not in percent("18.25")


class TestIdentifiers:
    """Account numbers: the sequence matters, so reading it as a quantity is a
    comprehension failure, not a style choice."""

    def test_digit_by_digit(self):
        assert digits("9157114007").split() == [
            "नौ", "एक", "पाँच", "सात", "एक", "एक", "चार", "शून्य", "शून्य", "सात",
        ]

    def test_no_spurious_leading_zero(self):
        """NeMo's Hindi telephone grammar inserts a leading शून्य on 10-digit numbers —
        11 spoken digits for 10 input. That would systematically corrupt exact-recovery
        scoring, so we never route identifiers through it."""
        assert len(digits("9157114007").split()) == 10

    def test_leading_zeros_preserved(self):
        assert digits("007").split() == ["शून्य", "शून्य", "सात"]

    def test_strips_formatting(self):
        assert digits("9157-114-007") == digits("9157114007")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            digits("no-digits-here")


class TestTenure:
    def test_anchor_and_confirm_on_whole_years(self):
        assert tenure_months(36) == f"{cardinal(36)} महीने, यानी {cardinal(3)} साल"

    def test_no_confirm_on_partial_year(self):
        assert tenure_months(18) == f"{cardinal(18)} महीने"

    def test_under_a_year(self):
        assert "साल" not in tenure_months(6)


class TestKFSValues:
    """Smoke test over the six categories the eval measures."""

    def test_all_categories_produce_devanagari(self):
        outs = [
            rupees("₹1,25,000"),
            percent("18.5"),
            tenure_months(36),
            rupees("₹4,382"),
            digits("9157114007"),
        ]
        for o in outs:
            assert o and not any(c.isdigit() for c in o), f"digits left in {o!r}"
