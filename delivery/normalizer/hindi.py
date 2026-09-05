"""Indic verbalisation of the financial values in an RBI Key Facts Statement.

This module is the project's central claim: Coda exposes no phoneme control, no SSML, no
pause tags and no lexicon, and Hindi exists on no other Rime model — so the text layer is
the only lever there is.

Division of labour. `indic-numtowords` (AI4Bharat, MIT) does integer -> Hindi words. It is
maintained, handles the Indian 2-2-3 grouping natively, and is what AI4Bharat's own
production Indic-TTS pipeline uses. Everything below is the part no library does:

  1. percentages   — NeMo's Hindi grammar has NO % / प्रतिशत support at all (verified absent
                     from measure.py, graph_utils.py, unit.tsv and its test cases). APR is
                     the most comprehension-critical value in a KFS and it is the one class
                     nothing covers.
  2. money decimals — ₹123.57 is "...रुपए सत्तावन पैसे" (a cardinal), but bare 99.99 is
                     "निन्यानबे दशमलव नौ नौ" (digit-by-digit). Two rules for the same '.',
                     switched by the currency symbol. One naive rule gives a
                     wrong-but-plausible reading a Hindi speaker catches instantly.
  3. identifiers   — digit-by-digit. We do NOT route these through NeMo's telephone
                     grammar, which inserts a spurious leading शून्य on 10-digit numbers
                     (11 spoken digits for 10 input) and would systematically corrupt
                     exact-recovery scoring.
  4. tenure        — anchor-and-confirm: say it twice, two ways. Redundancy is an
                     intelligibility technique, not a hack.
  5. scale ceiling — indic-numtowords raises TypeError above ~10^8. Guarded, not trusted.

ponytail: one module, not the five files the design doc listed. These rules share a number
formatter and are edited together; splitting them costs imports and buys nothing.

Self-check:  uv run python -m delivery.normalizer.hindi
"""

from __future__ import annotations

import re
from decimal import Decimal

from indic_numtowords import num2words

# indic-numtowords raises TypeError (not a clean error) past this magnitude.
# A KFS never legitimately exceeds it; anything that does is a parsing bug upstream.
MAX_CARDINAL = 10**8

RUPEE = "रुपए"
PAISE = "पैसे"
PERCENT = "प्रतिशत"
DECIMAL_POINT = "दशमलव"

# Colloquial collapse. These are what a borrower actually says, and NeMo encodes them as
# first-class constants — evidence they are the natural spoken form, not a shortcut.
HALF_FORMS = {
    Decimal("1.5"): "डेढ़",
    Decimal("2.5"): "ढाई",
}


def cardinal(n: int) -> str:
    """Integer -> Hindi words, Indian scale. 125000 -> 'एक लाख पच्चीस हज़ार'."""
    if not isinstance(n, int):
        raise TypeError(f"cardinal() takes int, got {type(n).__name__}")
    if n < 0:
        return f"माइनस {cardinal(-n)}"
    if n >= MAX_CARDINAL:
        raise ValueError(
            f"{n} exceeds the verbaliser's safe range ({MAX_CARDINAL}); "
            "indic-numtowords raises TypeError past this point."
        )
    return num2words(n, lang="hi")


def digits(s: str) -> str:
    """Digit-by-digit, with शून्य for zero. '9157114007' -> 'नौ एक पाँच ... शून्य सात'.

    Used for account numbers, reference numbers, anything where the *sequence* matters and
    reading it as a quantity would be a comprehension failure. Rime reads a bare digit run
    digit-by-digit and a comma-grouped one as a quantity, so formatting alone flips this —
    we make it explicit rather than relying on that.
    """
    only = re.sub(r"\D", "", s)
    if not only:
        raise ValueError(f"no digits in {s!r}")
    return num2words(only, lang="hi", split=True)


def rupees(amount: int | float | str | Decimal) -> str:
    """₹1,25,000 -> 'एक लाख पच्चीस हज़ार रुपए'.  ₹123.57 -> '... रुपए सत्तावन पैसे'.

    The fractional part is a CARDINAL count of paise, not a digit-by-digit reading.
    This is the rule that differs from bare decimals and it is easy to get wrong.
    """
    d = _to_decimal(amount)
    if d < 0:
        return f"माइनस {rupees(-d)}"

    whole = int(d)
    paise = int((d - whole).scaleb(2).to_integral_value())

    if paise == 0:
        return f"{cardinal(whole)} {RUPEE}"
    return f"{cardinal(whole)} {RUPEE} {cardinal(paise)} {PAISE}"


def percent(value: int | float | str | Decimal, *, confirm: bool = True) -> str:
    """18.5% -> 'अठारह दशमलव पाँच प्रतिशत, यानी साढ़े अठारह प्रतिशत'.

    No library covers this. The decimal part is read digit-by-digit (NeMo gold:
    99.99 -> 'निन्यानबे दशमलव नौ नौ'), unlike the paise rule above.

    `confirm` appends the colloquial half-form for .5 rates, which is how an APR is
    actually spoken. Anchor-and-confirm: the same value twice, two ways.
    """
    d = _to_decimal(value)
    whole = int(d)
    frac = d - whole

    if frac == 0:
        return f"{cardinal(whole)} {PERCENT}"

    frac_digits = format(frac, "f").split(".")[1].rstrip("0") or "0"
    spoken = f"{cardinal(whole)} {DECIMAL_POINT} {digits(frac_digits)} {PERCENT}"

    if confirm and frac == Decimal("0.5"):
        spoken += f", यानी साढ़े {cardinal(whole)} {PERCENT}"
    return spoken


def tenure_months(n: int, *, confirm: bool = True) -> str:
    """36 -> 'छत्तीस महीने, यानी तीन साल'. Anchor-and-confirm on whole years."""
    base = f"{cardinal(n)} महीने"
    if not confirm or n < 12 or n % 12:
        return base

    years = n // 12
    if Decimal(years) in HALF_FORMS:  # unreachable for ints, kept for the .5 path below
        return f"{base}, यानी {HALF_FORMS[Decimal(years)]} साल"
    return f"{base}, यानी {cardinal(years)} साल"


def decimal_number(value: int | float | str | Decimal) -> str:
    """Bare decimal, no currency. 99.99 -> 'निन्यानबे दशमलव नौ नौ'."""
    d = _to_decimal(value)
    whole = int(d)
    frac = d - whole
    if frac == 0:
        return cardinal(whole)
    frac_digits = format(frac, "f").split(".")[1].rstrip("0") or "0"
    return f"{cardinal(whole)} {DECIMAL_POINT} {digits(frac_digits)}"


def _to_decimal(v: int | float | str | Decimal) -> Decimal:
    """Parse Indian-grouped strings and currency symbols into a Decimal.

    float goes via str so 18.5 does not arrive as 18.5000000000000071.
    """
    if isinstance(v, Decimal):
        return v
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(str(v))
    cleaned = re.sub(r"[₹,\s]|रु\.?|Rs\.?", "", str(v), flags=re.IGNORECASE)
    if not cleaned:
        raise ValueError(f"no number in {v!r}")
    return Decimal(cleaned)


def _demo() -> None:
    """Self-check. Gold forms cross-checked against NeMo's Hindi test data."""
    assert cardinal(125000) == "एक लाख पच्चीस हज़ार"
    assert cardinal(80000) == "अस्सी हज़ार"

    # Comma-grouped Indian input must parse — this is how a KFS writes it.
    assert rupees("₹1,25,000") == "एक लाख पच्चीस हज़ार रुपए"
    assert rupees(80000) == "अस्सी हज़ार रुपए"

    # The money-decimal rule: paise as a cardinal, NOT digit-by-digit.
    assert rupees("₹123.57") == "एक सौ तेईस रुपए सत्तावन पैसे"

    # The bare-decimal rule: digit-by-digit after दशमलव. Same '.', different rule.
    #
    # NOTE the orthographic variant: indic-numtowords emits निन्यानवे (व) where NeMo's gold
    # data has निन्यानबे (ब). Both are attested spellings of 99. Rime pronounces them
    # near-identically, so this is a SCORER problem, not a TTS problem — and it is exactly
    # why the eval scores parsed typed values rather than strings. Any string-equality
    # scorer would count a correct answer wrong here.
    assert decimal_number("99.99") == "निन्यानवे दशमलव नौ नौ"

    # Percentages — the class no library covers. Asserted structurally: the RULE is what
    # matters (whole part cardinal, दशमलव, then fraction digit-by-digit), and hardcoding
    # the library's orthography here would make this test brittle for no benefit.
    assert percent(14, confirm=False) == f"{cardinal(14)} {PERCENT}"
    assert percent("18.5", confirm=False) == f"{cardinal(18)} {DECIMAL_POINT} {cardinal(5)} {PERCENT}"
    assert percent("18.5").endswith(f"यानी साढ़े {cardinal(18)} {PERCENT}")

    # Identifiers: digit-by-digit, शून्य for zero, and crucially NO spurious leading
    # शून्य — the exact defect that would corrupt account-number recovery scoring.
    acct = digits("9157114007")
    assert acct.split() == [
        "नौ", "एक", "पाँच", "सात", "एक", "एक", "चार", "शून्य", "शून्य", "सात",
    ], acct

    assert tenure_months(36) == f"{cardinal(36)} महीने, यानी {cardinal(3)} साल"
    assert tenure_months(18) == f"{cardinal(18)} महीने"  # not a whole year: no confirm

    # The scale ceiling is guarded, not discovered at 2am.
    try:
        cardinal(10**9)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError past MAX_CARDINAL")

    for label, out in [
        ("sanctioned", rupees("₹1,25,000")),
        ("emi", rupees("₹4,382")),
        ("apr", percent("18.5")),
        ("tenure", tenure_months(36)),
        ("account", digits("9157114007")),
        ("fee w/ paise", rupees("₹1,888.50")),
    ]:
        print(f"  {label:>14}: {out}")
    print("\nall assertions passed")


if __name__ == "__main__":
    _demo()
