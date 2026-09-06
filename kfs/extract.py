"""PDF -> KFS. Deterministic table parsing, Pydantic-validated, rejects on schema failure.

The KFS is a *standardised* form (RBI/2024-25/18, Annex A), which is the only reason this
can be deterministic rather than an LLM extraction step. Every row is a labelled
parameter/value pair and the fee sub-table has fixed columns, so we read the table grid and
look labels up. No heuristics, no regex over prose, no model in the loop.

Two rules that are load-bearing:

  * `loan_account_no` is read as a STRING and never touched. `int("0004512378")` is
    4512378 — the leading zeros this whole product exists to preserve would be destroyed
    before the delivery layer ever saw them.
  * Anything the schema rejects raises. A KFS that does not validate is not a KFS we are
    willing to read to a borrower as fact.

Self-check:  uv run python -m kfs.extract
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pdfplumber

from kfs.schema import KFS, FeeLine, FloatingInfo, InstalmentDetails, InterestRate

PAYEE = {"re": "RE", "third party": "third_party"}
TIMING = {"one-time": "one_time", "recurring": "recurring"}


def extract(pdf_path: str | Path) -> KFS:
    """Parse a KFS PDF. Raises pydantic.ValidationError if it does not conform."""
    part1, fee_rows = _tables(pdf_path)

    floating = None
    if _text(part1, "Interest rate type").lower() != "fixed":
        floating = FloatingInfo(
            benchmark=_text(part1, "Benchmark"),
            spread_pct=_num(part1, "Spread (%)"),
            reset_periodicity_months=int(_num(part1, "Reset periodicity (months)")),
            impact_25bps_on_epi=_num(part1, "Impact of 25 bps change on EPI (Rs)"),
            impact_25bps_on_num_epis=int(_num(part1, "Impact of 25 bps change on no. of EPIs")),
        )

    return KFS(
        proposal_no=_text(part1, "Loan proposal number"),
        loan_type=_text(part1, "Type of loan"),
        # Never int(): leading zeros are the payload.
        loan_account_no=_text(part1, "Loan account number"),
        sanctioned_amount_inr=_num(part1, "Sanctioned loan amount (Rs)"),
        disbursal_mode="stages" if "stage" in _text(part1, "Disbursal schedule").lower() else "upfront",
        loan_term_months=int(_num(part1, "Loan term (months)")),
        instalments=InstalmentDetails(
            type_of_instalments=_text(part1, "Type of instalments"),
            number_of_epis=int(_num(part1, "Number of EPIs")),
            epi_amount_inr=_num(part1, "EPI amount (Rs)"),
            repayment_commencement=_text(part1, "Commencement of repayment"),
        ),
        interest=InterestRate(
            pct=_num(part1, "Interest rate (% p.a.)"),
            kind=_text(part1, "Interest rate type").lower(),
        ),
        floating=floating,
        fees=[_fee(r) for r in fee_rows],
        apr_pct=_num(part1, "Annual Percentage Rate (APR) (%)"),
        total_repayment_inr=_num(part1, "Total amount to be paid (Rs)"),
        cooling_off_period_days=int(_num(part1, "Cooling-off period (days)")),
        recovery_agent_clause=_text(part1, "Recovery agents"),
        grievance_officer_name=_text(part1, "Grievance redressal officer"),
        grievance_officer_phone=_text(part1, "Grievance redressal phone"),
    )


def _tables(pdf_path: str | Path) -> tuple[dict[str, str], list[list[str]]]:
    """(Part 1 as label -> value, fee sub-table rows without its header).

    The fee table is found by its header cell rather than by index, so a future Part 2
    table appearing between them does not silently shift the parse.
    """
    part1: dict[str, str] = {}
    fee_rows: list[list[str]] = []

    with pdfplumber.open(pdf_path) as pdf:
        tables = [t for page in pdf.pages for t in (page.extract_tables() or [])]

    if not tables:
        raise ValueError(f"{pdf_path}: no tables found — not a KFS in the expected format")

    for table in tables:
        # Identified by its header, not by index, so a future Part 2 table appearing
        # between them does not shift the parse. The header repeats on every fragment
        # (reportlab `repeatRows=1`), so a table split across pages still classifies.
        header = " ".join(_clean(c or "") for c in table[0]).lower()
        if "payable to" in header:
            fee_rows += [r for r in table[1:] if any(c for c in r)]
        else:
            for row in table:
                if len(row) >= 2 and row[0] and row[1] is not None:
                    part1[_clean(row[0])] = _clean(row[1])

    return part1, fee_rows


def _fee(row: list[str]) -> FeeLine:
    item, payee, timing, amount, pct, gst = (_clean(c or "") for c in row[:6])
    return FeeLine(
        item=item,
        payable_to=PAYEE[payee.lower()],
        timing=TIMING[timing.lower()],
        amount_inr=_parse_money(amount),
        percentage=_parse_money(pct),
        net_of_gst=gst.lower().startswith("y"),
    )


def _clean(cell: str) -> str:
    """Cell text with reportlab's soft wraps collapsed back to single spaces."""
    return re.sub(r"\s+", " ", cell).strip()


def _text(part1: dict[str, str], label: str) -> str:
    try:
        return part1[label]
    except KeyError:
        raise ValueError(f"KFS row missing: {label!r}. Found: {sorted(part1)}") from None


def _num(part1: dict[str, str], label: str) -> Decimal:
    value = _parse_money(_text(part1, label))
    if value is None:
        raise ValueError(f"KFS row {label!r} is not a number: {_text(part1, label)!r}")
    return value


def _parse_money(s: str) -> Decimal | None:
    """'11,800' -> Decimal('11800'). '-' and '' -> None (an absent sub-table cell)."""
    stripped = re.sub(r"[^\d.]", "", s)
    return Decimal(stripped) if stripped else None


def _demo() -> None:
    """Round-trip every generated PDF against the JSON fixture it came from."""
    import json

    root = Path(__file__).parent.parent / "fixtures/synthetic"
    pdfs = sorted((root / "pdf").glob("*.pdf"))
    if not pdfs:
        raise SystemExit("no PDFs — run: PYTHONPATH=. uv run python fixtures/synthetic/_to_pdf.py")

    for pdf in pdfs:
        got = extract(pdf)
        want = KFS.model_validate(json.loads((root / f"{pdf.stem}.json").read_text("utf-8")))
        assert got == want, f"{pdf.name} did not round-trip"
        print(f"  {pdf.name:<40} acct {got.loan_account_no:>16}  APR {got.apr_pct}%")

    print(f"\n{len(pdfs)} PDFs round-tripped exactly, leading zeros intact")


if __name__ == "__main__":
    _demo()
