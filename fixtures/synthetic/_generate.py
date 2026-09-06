"""Regenerate the ten synthetic KFS fixtures.

    PYTHONPATH=. uv run python fixtures/synthetic/_generate.py

ALL DATA IS SYNTHETIC. Lender names, borrower-facing identifiers and grievance contacts are
invented; the toll-free numbers are in the 1800-2xx block and belong to nobody.

The JSON is committed, not generated at test time — a fixture that changes when a library
changes is not a fixture. This script exists so the arithmetic is auditable and so the next
person can add an eleventh document without re-deriving APR by hand.

Two things are computed rather than typed in, because typing them in is how fixtures end up
internally inconsistent:

  * EMI from the nominal rate on the SANCTIONED amount (that is the contract), and
  * APR from the monthly IRR on the NET DISBURSED amount (that is the disclosure).

Account numbers deliberately vary in length (10-16) and several carry LEADING ZEROS. The
day-1 pilot showed raw TTS loses those: `000512348899` came back as `95058000000`. They are
in the fixtures because they are the failure the product exists to prevent.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

from kfs.finance import apr_pct, emi
from kfs.schema import KFS, FeeLine, FloatingInfo, InstalmentDetails, InterestRate

OUT = Path(__file__).parent

D = Decimal


def _fee(item, amount=None, pct=None, payee="RE", timing="one_time", net_of_gst=False):
    return FeeLine(
        item=item,
        amount_inr=D(amount) if amount is not None else None,
        percentage=D(pct) if pct is not None else None,
        payable_to=payee,
        timing=timing,
        net_of_gst=net_of_gst,
    )


# (file stem, loan type, sanctioned, nominal rate %, months, fees, account no, extras)
DOCS = [
    dict(
        stem="kfs_01_two_wheeler",
        proposal_no="TW/2026/000418",
        loan_type="Two-Wheeler Loan",
        sanctioned="85000", rate="19.50", months=24,
        # Four leading zeros. The single most fragile shape for raw TTS.
        account="0004512378",
        fees=[
            _fee("Processing fee", amount="2125"),
            _fee("Documentation charges", amount="590", net_of_gst=True),
            _fee("Vehicle insurance premium", amount="3400", payee="third_party"),
            _fee("Foreclosure charges", pct="4.00"),
        ],
        commencement="30 days from disbursal",
        cooling_off=3,
        grievance=("Sunita Rathore", "1800 200 4455"),
    ),
    dict(
        stem="kfs_02_personal",
        proposal_no="PL/2026/117203",
        loan_type="Personal Loan",
        sanctioned="300000", rate="15.75", months=48,
        account="915711400712",
        fees=[
            _fee("Processing fee", amount="7500"),
            _fee("Credit protect premium", amount="4800", payee="third_party"),
            _fee("Annual service charge", amount="600", timing="recurring"),
            _fee("Part-prepayment charges", pct="2.00"),
        ],
        commencement="1st of the month following disbursal",
        cooling_off=7,
        grievance=("Imran Qureshi", "1800 209 6612"),
    ),
    dict(
        stem="kfs_03_consumer_durable",
        proposal_no="CD/2026/884501",
        loan_type="Consumer Durable Loan",
        sanctioned="45000", rate="21.00", months=12,
        # Two leading zeros, odd length 11.
        account="00512348899",
        fees=[
            _fee("Processing fee", amount="1180", net_of_gst=True),
            _fee("Dealer subvention recovery", amount="900", payee="third_party"),
        ],
        commencement="30 days from disbursal",
        cooling_off=3,
        grievance=("Meera Nair", "1800 233 7788"),
    ),
    dict(
        stem="kfs_04_gold",
        proposal_no="GL/2026/305277",
        loan_type="Gold Loan",
        sanctioned="150000", rate="13.50", months=12,
        account="4020110005219",
        fees=[
            _fee("Processing fee", amount="750"),
            _fee("Gold valuation charges", amount="500", payee="third_party"),
            _fee("Auction notice charges", amount="350"),
        ],
        commencement="30 days from disbursal",
        cooling_off=0,
        grievance=("Rajesh Pillai", "1800 266 1190"),
    ),
    dict(
        # THE AWKWARD ONE: floating rate with a reset clause, the longest tenure, the
        # largest amount and a 15-digit account number with leading zeros.
        stem="kfs_05_small_business_floating",
        proposal_no="SB/2026/046612",
        loan_type="Small Business Loan",
        sanctioned="500000", rate="17.25", months=60,
        account="000900081234567",
        fees=[
            _fee("Processing fee", amount="11800", net_of_gst=True),
            _fee("Stamp duty", amount="2500", payee="third_party"),
            _fee("Annual renewal fee", amount="1500", timing="recurring"),
            _fee("Foreclosure charges", pct="3.00"),
        ],
        commencement="45 days from first disbursal",
        cooling_off=7,
        grievance=("Anand Deshpande", "1800 274 3021"),
        disbursal_mode="stages",
        floating=dict(benchmark="Lender's 6-month MCLR", spread="4.75", reset_months=6),
    ),
    dict(
        stem="kfs_06_personal_small",
        proposal_no="PL/2026/117884",
        loan_type="Personal Loan",
        sanctioned="25000", rate="24.00", months=12,
        account="7391028465",
        fees=[
            _fee("Processing fee", amount="885"),
            _fee("Late payment charges", pct="2.00", timing="recurring"),
        ],
        commencement="30 days from disbursal",
        cooling_off=3,
        grievance=("Kavita Joshi", "1800 288 5140"),
    ),
    dict(
        stem="kfs_07_two_wheeler_long",
        proposal_no="TW/2026/000933",
        loan_type="Two-Wheeler Loan",
        # The pilot's own number, kept so the fixtures and the battery clips line up.
        sanctioned="104596", rate="20.75", months=36,
        account="0100234567891234",
        fees=[
            _fee("Processing fee", amount="2620"),
            _fee("RTO / hypothecation charges", amount="1800", payee="third_party"),
            _fee("Vehicle insurance premium", amount="4150", payee="third_party"),
        ],
        commencement="30 days from disbursal",
        cooling_off=3,
        grievance=("Sunita Rathore", "1800 200 4455"),
    ),
    dict(
        stem="kfs_08_consumer_durable_large",
        proposal_no="CD/2026/885417",
        loan_type="Consumer Durable Loan",
        sanctioned="68500", rate="24.50", months=18,
        account="58210037461902",
        fees=[
            _fee("Processing fee", amount="2360", net_of_gst=True),
            _fee("Extended warranty premium", amount="2100", payee="third_party"),
            _fee("Cheque bounce charges", amount="590", timing="recurring"),
        ],
        commencement="30 days from disbursal",
        cooling_off=3,
        grievance=("Meera Nair", "1800 233 7788"),
    ),
    dict(
        stem="kfs_09_gold_large",
        proposal_no="GL/2026/305901",
        loan_type="Gold Loan",
        sanctioned="225000", rate="14.25", months=24,
        # Four leading zeros again, at a different length.
        account="0000123456789",
        fees=[
            _fee("Processing fee", amount="1125"),
            _fee("Gold valuation charges", amount="700", payee="third_party"),
        ],
        commencement="30 days from disbursal",
        cooling_off=0,
        grievance=("Rajesh Pillai", "1800 266 1190"),
    ),
    dict(
        stem="kfs_10_small_business",
        proposal_no="SB/2026/047190",
        loan_type="Small Business Loan",
        sanctioned="410000", rate="18.90", months=36,
        account="612345098765432",
        fees=[
            _fee("Processing fee", amount="9676"),
            _fee("Stamp duty", amount="2050", payee="third_party"),
            _fee("Annual renewal fee", amount="1200", timing="recurring"),
            _fee("Foreclosure charges", pct="4.00"),
        ],
        commencement="30 days from disbursal",
        cooling_off=7,
        grievance=("Anand Deshpande", "1800 274 3021"),
    ),
]

RECOVERY_CLAUSE = (
    "Recovery agents are engaged only from the panel published on the lender's website, "
    "and may contact the borrower between 8 AM and 7 PM."
)


def build(spec: dict) -> KFS:
    sanctioned = D(spec["sanctioned"])
    months = spec["months"]
    rate = D(spec["rate"])
    fees: list[FeeLine] = spec["fees"]

    instalment = emi(sanctioned, rate, months)
    upfront = sum((f.amount_inr or D(0)) for f in fees if f.timing == "one_time")
    # Recurring fees are annual; a 36-month loan pays three of them.
    years = math.ceil(months / 12)
    recurring = sum((f.amount_inr or D(0)) for f in fees if f.timing == "recurring")

    floating = spec.get("floating")
    floating_info = None
    if floating:
        # RBI Annex A row 10: the effect of a 25 bps move, disclosed both ways —
        # on the EMI if the tenure is held, and on the tenure if the EMI is held.
        bumped = emi(sanctioned, rate + D("0.25"), months)
        floating_info = FloatingInfo(
            benchmark=floating["benchmark"],
            spread_pct=D(floating["spread"]),
            reset_periodicity_months=floating["reset_months"],
            impact_25bps_on_epi=bumped - instalment,
            impact_25bps_on_num_epis=_months_at_fixed_epi(sanctioned, rate + D("0.25"), instalment) - months,
        )

    name, phone = spec["grievance"]
    return KFS(
        proposal_no=spec["proposal_no"],
        loan_type=spec["loan_type"],
        sanctioned_amount_inr=sanctioned,
        disbursal_mode=spec.get("disbursal_mode", "upfront"),
        loan_term_months=months,
        instalments=InstalmentDetails(
            number_of_epis=months,
            epi_amount_inr=instalment,
            repayment_commencement=spec["commencement"],
        ),
        interest=InterestRate(pct=rate, kind="floating" if floating else "fixed"),
        floating=floating_info,
        fees=fees,
        apr_pct=apr_pct(sanctioned - D(upfront), instalment, months),
        total_repayment_inr=instalment * months + D(upfront) + D(recurring) * years,
        cooling_off_period_days=spec["cooling_off"],
        recovery_agent_clause=RECOVERY_CLAUSE,
        grievance_officer_name=name,
        grievance_officer_phone=phone,
        loan_account_no=spec["account"],
    )


def _months_at_fixed_epi(principal: Decimal, annual_rate_pct: Decimal, instalment: Decimal) -> int:
    """How many instalments the loan runs to if the EMI is frozen and the rate moves."""
    i = float(annual_rate_pct) / 1200.0
    p, e = float(principal), float(instalment)
    n = 0
    while p > 0.5 and n < 1200:
        p = p * (1 + i) - e
        n += 1
    return n


def main() -> None:
    for spec in DOCS:
        doc = build(spec)
        path = OUT / f"{spec['stem']}.json"
        path.write_text(doc.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(
            f"  {spec['stem']:<34} Rs {doc.sanctioned_amount_inr:>9}  "
            f"{doc.interest.pct}%  {doc.loan_term_months}m  "
            f"EMI {doc.instalments.epi_amount_inr:>7}  APR {doc.apr_pct}%  "
            f"acct {doc.loan_account_no}"
        )
        assert D("14") <= doc.apr_pct <= D("34"), f"{spec['stem']} APR out of range"
    print(f"\nwrote {len(DOCS)} fixtures to {OUT}")


if __name__ == "__main__":
    main()
