"""RBI Key Facts Statement, Annex A. Circular RBI/2024-25/18.

Modelled NESTED because Annex A is nested. Part 1 is not ten flat fields: row 1 carries
two fields under one serial number, and rows 3, 5, 7, 8 and 10 are sub-tables. A flat
model would misrepresent the regulation and make the read-aloud order wrong.

Two facts that are easy to get wrong and are load-bearing for a consent product:

  * APR is the NOMINAL annualised rate — monthly IRR x 12, NOT compounded. Verified
    against RBI's own Annex B illustration: monthly IRR 1.4225% x 12 = 17.0706%, published
    as 17.07%.
  * APR is computed on the amount NET OF ALL FEES actually disbursed, not on the
    sanctioned amount. Annex B row 7 is literally "Net disbursed amount".

RBI's own Annex C illustration does not foot (principal column sums to 19,999 against a
20,000 loan). Do not "fix" fixtures to match RBI's arithmetic; match the schema instead.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

FeeTiming = Literal["one_time", "recurring"]
Payee = Literal["RE", "third_party"]  # RE = Regulated Entity (the lender)


class FeeLine(BaseModel):
    """One row of the fees sub-table. Amount OR percentage, not both.

    Annex A footnote 4 permits disclosure net of taxes, so the figure read aloud may be
    Rs 1,600 while Rs 1,888 actually leaves the borrower's account. `net_of_gst` records
    which it is, because a borrower who hears one and is debited the other has been
    misinformed even though the lender complied.
    """

    item: str
    payable_to: Payee = "RE"
    timing: FeeTiming = "one_time"
    amount_inr: Decimal | None = None
    percentage: Decimal | None = None
    net_of_gst: bool = False


class InstalmentDetails(BaseModel):
    type_of_instalments: str = "Monthly"
    number_of_epis: int  # Equated Periodic Instalments
    epi_amount_inr: Decimal
    repayment_commencement: str  # e.g. "30 days from disbursal"


class InterestRate(BaseModel):
    pct: Decimal
    kind: Literal["fixed", "floating", "hybrid"] = "fixed"


class FloatingInfo(BaseModel):
    """Only present when InterestRate.kind is floating or hybrid."""

    benchmark: str
    spread_pct: Decimal
    reset_periodicity_months: int
    impact_25bps_on_epi: Decimal | None = None
    impact_25bps_on_num_epis: int | None = None


class KFS(BaseModel):
    """Annex A Part 1. Part 2 items 5-6 are permissive ("may be furnished") and omitted."""

    # Row 1 — two fields, one serial number
    proposal_no: str
    loan_type: str

    sanctioned_amount_inr: Decimal
    disbursal_mode: Literal["upfront", "stages"] = "upfront"
    loan_term_months: int

    instalments: InstalmentDetails
    interest: InterestRate
    floating: FloatingInfo | None = None

    fees: list[FeeLine] = Field(default_factory=list)

    apr_pct: Decimal
    total_repayment_inr: Decimal
    cooling_off_period_days: int = 0

    recovery_agent_clause: str = ""
    grievance_officer_name: str = ""
    grievance_officer_phone: str = ""

    # The identifier the borrower is asked to verify. This is the category the day-1 pilot
    # found Rime reads as a QUANTITY rather than a sequence (raw 1/5 vs delivery 5/5),
    # so it is the primary claim's payload.
    loan_account_no: str = ""

    @property
    def net_disbursed_inr(self) -> Decimal:
        """Sanctioned minus all one-time fees. The base APR is computed on."""
        upfront = sum(
            (f.amount_inr or Decimal(0)) for f in self.fees if f.timing == "one_time"
        )
        return self.sanctioned_amount_inr - Decimal(upfront)
