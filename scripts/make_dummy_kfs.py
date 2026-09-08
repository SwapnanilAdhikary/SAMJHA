"""Generate a dummy KFS to upload, as a lender would actually hand you one.

    uv run python scripts/make_dummy_kfs.py                  # messy .docx in /tmp
    uv run python scripts/make_dummy_kfs.py --clean          # every label canonical
    uv run python scripts/make_dummy_kfs.py --format both
    uv run python scripts/make_dummy_kfs.py --seed 7 --out ~/kfs.pdf --format pdf

The ten documents in `fixtures/synthetic/` are generated FROM the same label list
`kfs/extract.py` reads, so they round-trip by construction and exercise none of the
interesting paths. This makes something deliberately unlike them:

  * **its own label spellings** — "Loan Proposal No.", "APR (%)", "Sanctioned Amount (Rs.)"
    — so `kfs/fields.py`'s synonym table has to earn its place;
  * **a label nobody should guess at** ("Total charges (Rs)"), which must be reported as
    unrecognised rather than folded into "Total amount to be paid";
  * **a missing required row** (the cooling-off period by default), so the intake page has
    to ask a human for it instead of letting `KFS`'s default of 0 be read aloud as
    "इस लोन में कूलिंग-ऑफ की अवधि नहीं है।" — a denial of a statutory right.

The loan itself is internally consistent: the EMI is computed with `kfs.finance.emi` and
the APR is solved with `kfs.finance.apr_pct` over the amount net of one-time fees, so the
document does not contradict itself the way a hand-typed specimen would.

STILL SYNTHETIC. Every value is generated from `--seed`; there is no real borrower here,
and the upload path defaults to declaring documents synthetic regardless.
"""

from __future__ import annotations

import argparse
import random
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fixtures.synthetic import _to_docx as D  # noqa: E402
from fixtures.synthetic._to_pdf import fee_table, part1_rows, render as render_pdf  # noqa: E402
from kfs.finance import apr_pct, emi  # noqa: E402
from kfs.schema import KFS, FeeLine, InstalmentDetails, InterestRate  # noqa: E402

# A real lender's own wording for rows Annex A names differently. This is the drift the
# synonym table claims to absorb; generating it is how that claim gets tested.
DRIFT = {
    "Loan proposal number": "Loan Proposal No.",
    "Sanctioned loan amount (Rs)": "Sanctioned Amount (Rs.)",
    "Annual Percentage Rate (APR) (%)": "APR (%)",
    "Loan term (months)": "Loan Tenure (months)",
    "Number of EPIs": "No. of EPIs",
    "Grievance redressal officer": "Nodal Officer",
}

# A row that is NOT an Annex-A field. "Total charges" is not "Total amount to be paid",
# and guessing that it is would put a wrong number in a consent record.
DECOY = ("Total charges (Rs)", "14,720")

LOAN_TYPES = ("Two-Wheeler Loan", "Personal Loan", "Consumer Durable Loan",
              "Gold Loan", "Small Business Loan")


def build(seed: int) -> KFS:
    """A plausible, internally consistent loan. Deterministic in `seed`."""
    rng = random.Random(seed)

    principal = Decimal(rng.choice([65000, 85000, 120000, 145000, 210000, 340000]))
    rate = Decimal(rng.choice(["13.5", "15.25", "17.9", "19.5", "22.0", "24.75"]))
    months = rng.choice([12, 18, 24, 36, 48, 60])
    # Leading zeros are the payload: read as a string, never int().
    account = rng.choice(["000", "0000", "00", ""]) + str(rng.randint(10**8, 10**12))

    instalment = emi(principal, rate, months)
    processing = Decimal(rng.choice([885, 1180, 1750, 2500]))
    insurance = Decimal(rng.choice([0, 1888, 2400]))

    fees = [FeeLine(item="Processing fee", payable_to="RE", timing="one_time",
                    amount_inr=processing, net_of_gst=True)]
    if insurance:
        fees.append(FeeLine(item="Insurance premium", payable_to="third_party",
                            timing="one_time", amount_inr=insurance))
    fees.append(FeeLine(item="Late payment charge", payable_to="RE", timing="recurring",
                        percentage=Decimal("2")))

    upfront = sum((f.amount_inr or Decimal(0)) for f in fees if f.timing == "one_time")
    net = principal - Decimal(upfront)

    return KFS(
        proposal_no=f"{rng.choice(('PL', 'TW', 'CD', 'GL', 'SB'))}/2026/{rng.randint(100000, 999999)}",
        loan_type=rng.choice(LOAN_TYPES),
        loan_account_no=account,
        sanctioned_amount_inr=principal,
        disbursal_mode="upfront",
        loan_term_months=months,
        instalments=InstalmentDetails(
            type_of_instalments="Monthly",
            number_of_epis=months,
            epi_amount_inr=instalment,
            repayment_commencement="30 days from disbursal",
        ),
        interest=InterestRate(pct=rate, kind="fixed"),
        fees=fees,
        apr_pct=apr_pct(net, instalment, months),
        total_repayment_inr=instalment * months,
        cooling_off_period_days=rng.choice([3, 7]),
        recovery_agent_clause=(
            "Recovery agents engaged by the lender may contact the borrower between "
            "08:00 and 19:00 hours. The borrower may lodge a complaint with the "
            "grievance redressal officer named below."
        ),
        grievance_officer_name=rng.choice(
            ("Smt. Anita Sharma", "Shri R. Venkatesan", "Ms. Farida Khan")),
        grievance_officer_phone=f"1800 {rng.randint(200, 299)} {rng.randint(1000, 9999)}",
    )


def rows_for(kfs: KFS, *, clean: bool, omit: list[str]) -> list[list[str]]:
    out = []
    for label, value in part1_rows(kfs):
        if label in omit:
            continue
        out.append([label if clean else DRIFT.get(label, label), value])
    if not clean:
        out.append(list(DECOY))
    return out


def write_docx(kfs: KFS, path: Path, rows: list[list[str]]) -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{D.W}"><w:body>'
        + D._para("KEY FACTS STATEMENT")
        + D._para("Issued under RBI/2024-25/18. SYNTHETIC SPECIMEN, NOT A REAL LOAN.")
        + D._table(rows)
        + D._para()
        + D._para("Fees and charges")
        + D._table(fee_table(kfs))
        + "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", D.CONTENT_TYPES)
        z.writestr("_rels/.rels", D.RELS)
        z.writestr("word/document.xml", xml)


def write_pdf(kfs: KFS, path: Path, rows: list[list[str]]) -> None:
    """Reuses _to_pdf.render by swapping the row list it would have produced."""
    import fixtures.synthetic._to_pdf as P  # noqa: PLC0415

    original = P.part1_rows
    P.part1_rows = lambda _k: [(a, b) for a, b in rows]
    try:
        render_pdf(kfs, path)
    finally:
        P.part1_rows = original


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a dummy KFS to upload.")
    ap.add_argument("--out", type=Path, default=None,
                    help="output path (default: /tmp/dummy_kfs.<ext>)")
    ap.add_argument("--format", choices=("docx", "pdf", "both"), default="docx")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--clean", action="store_true",
                    help="canonical labels, nothing missing — the easy path")
    ap.add_argument("--omit", action="append", default=None, metavar="LABEL",
                    help="drop a required row; repeatable "
                         "(default: 'Cooling-off period (days)' unless --clean)")
    args = ap.parse_args()

    omit = args.omit if args.omit is not None else ([] if args.clean
                                                    else ["Cooling-off period (days)"])
    kfs = build(args.seed)
    rows = rows_for(kfs, clean=args.clean, omit=omit)

    formats = ("docx", "pdf") if args.format == "both" else (args.format,)
    written = []
    for fmt in formats:
        path = (args.out if args.out and args.format != "both"
                else Path(f"/tmp/dummy_kfs.{fmt}"))
        if args.out and args.format == "both":
            path = args.out.with_suffix(f".{fmt}")
        path.parent.mkdir(parents=True, exist_ok=True)
        (write_docx if fmt == "docx" else write_pdf)(kfs, path, rows)
        written.append(path)

    print(f"{kfs.loan_type} · {kfs.proposal_no}")
    print(f"  sanctioned   ₹{kfs.sanctioned_amount_inr}")
    print(f"  account no   {kfs.loan_account_no}   <- leading zeros are the payload")
    print(f"  {kfs.interest.pct}% p.a. over {kfs.loan_term_months} months, "
          f"EMI ₹{kfs.instalments.epi_amount_inr}, APR {kfs.apr_pct}%")
    print(f"  cooling-off  {kfs.cooling_off_period_days} days"
          + ("  <- OMITTED from the document" if "Cooling-off period (days)" in omit else ""))

    if not args.clean:
        print("\n  this document is deliberately messy:")
        for canonical, printed in DRIFT.items():
            if canonical not in omit:
                print(f"    {printed!r:<32} instead of {canonical!r}")
        print(f"    {DECOY[0]!r:<32} is not an Annex-A field and must not be guessed at")
        for label in omit:
            print(f"    {label!r} is absent — a human has to supply it")

    for path in written:
        print(f"\nwrote {path}")
    print("\nupload it at http://localhost:8000/intake , or:")
    print(f'  make e2e ARGS="--doc {written[0]} --drive'
          + ("" if args.clean else ' --supply \\"Cooling-off period (days)=3\\"') + '"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
