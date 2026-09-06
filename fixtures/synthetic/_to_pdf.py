"""Render the JSON fixtures as KFS PDFs, so kfs/extract.py has something real to parse.

    PYTHONPATH=. uv run python fixtures/synthetic/_to_pdf.py

Deliberately plain: a bordered two-column Part 1 table plus a bordered fee sub-table, which
is what Annex A actually is. No styling, because the point is a document pdfplumber can
read the way it reads a lender's, not a pretty one.

The PDF is in English. That is correct, not a shortcut — the KFS is filed in English and
the Hindi is what SAMJHA *produces* from it. The regulatory hook ("shall be written in a
language understood by the borrowers") is precisely the gap this project fills.

⚠️ Amounts are printed in Indian 2-2-3 grouping WITH commas, and account numbers keep their
leading zeros, because both are what breaks naive parsers. `Rs`, not `₹`: reportlab's
built-in Helvetica has no U+20B9 glyph and would emit a black box.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from kfs.build_clauses import inr
from kfs.schema import KFS

HERE = Path(__file__).parent
OUT = HERE / "pdf"

CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=8, leading=10)
BOLD = ParagraphStyle("bold", fontName="Helvetica-Bold", fontSize=8, leading=10)
TITLE = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=11, leading=14)

GRID = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
])


def part1_rows(k: KFS) -> list[tuple[str, str]]:
    """Label text here must match kfs/extract.py exactly. The round-trip test is the guard."""
    i, f = k.instalments, k.floating
    rows = [
        ("Loan proposal number", k.proposal_no),
        ("Type of loan", k.loan_type),
        ("Loan account number", k.loan_account_no),
        ("Sanctioned loan amount (Rs)", inr(k.sanctioned_amount_inr)),
        ("Disbursal schedule", "Upfront, in full" if k.disbursal_mode == "upfront" else "In stages"),
        ("Loan term (months)", str(k.loan_term_months)),
        ("Type of instalments", i.type_of_instalments),
        ("Number of EPIs", str(i.number_of_epis)),
        ("EPI amount (Rs)", inr(i.epi_amount_inr)),
        ("Commencement of repayment", i.repayment_commencement),
        ("Interest rate (% p.a.)", f"{k.interest.pct}"),
        ("Interest rate type", k.interest.kind.capitalize()),
    ]
    if f is not None:
        rows += [
            ("Benchmark", f.benchmark),
            ("Spread (%)", f"{f.spread_pct}"),
            ("Reset periodicity (months)", str(f.reset_periodicity_months)),
            ("Impact of 25 bps change on EPI (Rs)", inr(f.impact_25bps_on_epi or Decimal(0))),
            ("Impact of 25 bps change on no. of EPIs", str(f.impact_25bps_on_num_epis or 0)),
        ]
    rows += [
        ("Annual Percentage Rate (APR) (%)", f"{k.apr_pct}"),
        ("Total amount to be paid (Rs)", inr(k.total_repayment_inr)),
        ("Cooling-off period (days)", str(k.cooling_off_period_days)),
        ("Recovery agents", k.recovery_agent_clause),
        ("Grievance redressal officer", k.grievance_officer_name),
        ("Grievance redressal phone", k.grievance_officer_phone),
    ]
    return rows


def fee_table(k: KFS) -> list[list[str]]:
    header = ["Fee or charge", "Payable to", "Timing", "Amount (Rs)", "Percentage (%)", "Net of GST"]
    rows = [header]
    for fee in k.fees:
        rows.append([
            fee.item,
            "RE" if fee.payable_to == "RE" else "Third party",
            "One-time" if fee.timing == "one_time" else "Recurring",
            inr(fee.amount_inr) if fee.amount_inr is not None else "-",
            f"{fee.percentage}" if fee.percentage is not None else "-",
            "Yes" if fee.net_of_gst else "No",
        ])
    return rows


def render(k: KFS, path: Path) -> None:
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Key Facts Statement — {k.proposal_no}",
    )
    main = Table(
        [[Paragraph(label, BOLD), Paragraph(value, CELL)] for label, value in part1_rows(k)],
        colWidths=[62 * mm, 112 * mm],
    )
    main.setStyle(GRID)

    fees_rows = fee_table(k)
    fees = Table(
        [[Paragraph(c, BOLD if r == 0 else CELL) for c in row] for r, row in enumerate(fees_rows)],
        colWidths=[54 * mm, 22 * mm, 22 * mm, 26 * mm, 26 * mm, 24 * mm],
        repeatRows=1,  # the header must repeat, or a page split hides it from the parser
    )
    fees.setStyle(GRID)

    doc.build([
        Paragraph("KEY FACTS STATEMENT", TITLE),
        Paragraph("RBI/2024-25/18, Annex A — Part 1. SYNTHETIC SPECIMEN, NOT A REAL LOAN.", CELL),
        Spacer(1, 5 * mm),
        main,
        Spacer(1, 5 * mm),
        Paragraph("Fees and charges", TITLE),
        Spacer(1, 2 * mm),
        fees,
    ])


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for src in sorted(HERE.glob("*.json")):
        doc = KFS.model_validate(json.loads(src.read_text(encoding="utf-8")))
        dest = OUT / f"{src.stem}.pdf"
        render(doc, dest)
        print(f"  {dest.relative_to(HERE.parent.parent)}")
    print(f"\nwrote {len(list(OUT.glob('*.pdf')))} PDFs")


if __name__ == "__main__":
    main()
