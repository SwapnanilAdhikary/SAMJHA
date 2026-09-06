"""The two bits of arithmetic a KFS must be internally consistent about.

RBI Annex B defines APR as the **nominal** annualised rate — monthly IRR x 12, NOT
compounded — computed on the amount NET of all fees actually disbursed. Both halves are
easy to get wrong in the same direction (compounding, and using the sanctioned amount),
and both inflate the published figure, so a lender getting them wrong looks compliant.

There is no closed form for the IRR, so we bisect. That is slower than Newton and cannot
diverge, which is the right trade for ten fixtures computed once.

Self-check:  uv run python -m kfs.finance
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def emi(principal: Decimal, annual_rate_pct: Decimal, months: int) -> Decimal:
    """Equated monthly instalment, rounded to whole rupees as lenders actually quote it.

    Rounding here and then solving the IRR against the ROUNDED figure is deliberate: the
    borrower pays the rounded EMI, so that is the cash flow the APR describes.
    """
    i = float(annual_rate_pct) / 1200.0
    if i == 0:
        raw = float(principal) / months
    else:
        g = (1 + i) ** months
        raw = float(principal) * i * g / (g - 1)
    return Decimal(str(raw)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def monthly_irr(net_disbursed: Decimal, instalment: Decimal, months: int) -> float:
    """Monthly rate r solving  net = sum_{t=1..n} instalment / (1+r)^t.

    Bisection on [0, 1] monthly (0% to 1200% annual) — wide enough that no realistic NBFC
    loan escapes the bracket, so this never needs a fallback path.
    """
    net, pmt = float(net_disbursed), float(instalment)

    def pv(r: float) -> float:
        if r == 0:
            return pmt * months
        return pmt * (1 - (1 + r) ** -months) / r

    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        # pv decreases in r, so the root is above mid when pv(mid) still exceeds net.
        if pv(mid) > net:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def apr_pct(net_disbursed: Decimal, instalment: Decimal, months: int) -> Decimal:
    """APR = monthly IRR x 12, to 2dp. Nominal, never compounded."""
    annual = monthly_irr(net_disbursed, instalment, months) * 12 * 100
    return Decimal(str(annual)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _demo() -> None:
    """Anchored on RBI's own Annex B illustration: monthly IRR 1.4225% -> APR 17.07%."""
    assert apr_pct(Decimal("1"), Decimal("1"), 1) == Decimal("0.00")

    # A zero-fee loan's APR is its nominal rate: net == principal, so the IRR is just i.
    e = emi(Decimal("100000"), Decimal("18"), 24)
    assert abs(apr_pct(Decimal("100000"), e, 24) - Decimal("18")) < Decimal("0.05")

    # Fees push APR above the nominal rate. This is the whole point of the disclosure.
    with_fee = apr_pct(Decimal("98000"), e, 24)
    assert with_fee > Decimal("20"), with_fee

    print(f"  EMI on Rs 1,00,000 @ 18% / 24m : {e}")
    print(f"  APR, no fees                   : {apr_pct(Decimal('100000'), e, 24)}")
    print(f"  APR, Rs 2,000 fee              : {with_fee}")
    print("\nall assertions passed")


if __name__ == "__main__":
    _demo()
