"""Generate the eval corpus: 10 synthetic KFS documents and 120 typed utterances.

All data is SYNTHETIC. No real borrower data, ever.

`fixtures/synthetic/` was empty when this was written, so the documents are generated here
from `kfs.schema.KFS` rather than lifted from that workstream. Values are internally
consistent — the EMI is the annuity payment on the sanctioned amount, and the APR is the
monthly IRR on the amount NET OF FEES times twelve (nominal, not compounded), which is
RBI's rule and the one thing about a KFS that is easiest to get wrong.

Each utterance carries BOTH arm texts, baked in and committed, so the A/B is inspectable
without running anything:

  carrier_a  — the clause as a KFS writes it: `₹1,25,000`, `18.5%`, `000512348899`
  carrier_b  — the same clause with the value verbalised by delivery/normalizer/hindi.py

Carriers are native Hindi sentences, ten per category, cycled with a per-document offset so
no template repeats more than twelve times across the corpus.

    uv run python evals/corpus/build_corpus.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from delivery.normalizer import hindi  # noqa: E402
from evals.asr_score import MONTHS_HI  # noqa: E402
from kfs.schema import KFS, FeeLine, InstalmentDetails, InterestRate  # noqa: E402

HERE = Path(__file__).resolve().parent

# One value per document per slot. Chosen for spread, not realism-theatre: magnitudes from
# tens of thousands to tens of lakhs, rates with 0, 1 and 2 decimal places, tenures that are
# and are not whole years (the delivery layer's anchor-and-confirm only fires on whole ones).
DOCS = [
    # sanctioned, rate%, months, processing fee, insurance fee, disbursed on
    (125000, "18.5", 36, 2500, 1888, date(2026, 3, 15)),
    (80000, "14", 24, 1600, 0, date(2026, 4, 1)),
    (350000, "12.75", 60, 5250, 4200, date(2026, 1, 20)),
    (45000, "22.5", 18, 900, 0, date(2026, 6, 5)),
    (1250000, "9.6", 84, 12500, 9500, date(2026, 2, 28)),
    (60000, "24", 12, 1200, 0, date(2026, 7, 10)),
    (275000, "16.25", 48, 4125, 3300, date(2026, 5, 12)),
    (999000, "11.99", 72, 9990, 0, date(2026, 8, 22)),
    (32000, "26.5", 9, 640, 0, date(2026, 9, 3)),
    (640000, "13.4", 30, 9600, 5120, date(2026, 10, 18)),
]

# Account identifiers. LEADING ZEROS ARE THE HEADLINE — the day-1 pilot lost them entirely
# on the raw arm (000512348899 came back as 95058000000), so half of these start with one.
ACCOUNTS = [
    "000512348899", "9157114007", "0079412356", "402011000521", "50100247865321",
    "0000000123", "1234500067", "000100024578", "8890120034", "0450078123",
    "30201045678900", "0501004598", "6002310045678", "0100200300", "912300045601",
    "7788990011", "000900081234", "5012340009876", "0000345612", "00081234567890",
]

# Second tenure per document: not a KFS field, a varied quoted period. Labelled as such.
ALT_TENURES = [6, 15, 21, 3, 42, 54, 27, 33, 39, 45]

CARRIERS: dict[str, list[str]] = {
    "rupee_amount": [
        "आपके लोन की कुल स्वीकृत राशि {v} है।",
        "बैंक ने आपको {v} का ऋण मंज़ूर किया है।",
        "इस ऋण की मूल राशि {v} है।",
        "आपके खाते में {v} जमा किए जाएँगे।",
        "कुल चुकाने योग्य रकम {v} बनती है।",
        "मंज़ूर की गई रकम {v} है, कृपया ध्यान से सुनिए।",
        "आपको कुल {v} वापस करने होंगे।",
        "ऋण की राशि {v} तय हुई है।",
        "यह लोन {v} का है।",
        "आपके नाम पर {v} की राशि स्वीकृत हुई है।",
    ],
    "percentage_apr": [
        "आपके लोन की वार्षिक ब्याज दर {v} है।",
        "सालाना प्रतिशत दर यानी एपीआर {v} है।",
        "ब्याज {v} की दर से लगेगा।",
        "इस ऋण पर ब्याज दर {v} रहेगी।",
        "कुल वार्षिक लागत दर {v} है।",
        "आपसे {v} सालाना ब्याज लिया जाएगा।",
        "ब्याज की दर {v} निर्धारित की गई है।",
        "वार्षिक दर {v} है और यह स्थिर दर है।",
        "आपके ऋण की एपीआर {v} है।",
        "ब्याज दर {v} प्रति वर्ष है।",
    ],
    "tenure_months": [
        "इस लोन की अवधि {v} है।",
        "आपको यह ऋण {v} में चुकाना है।",
        "भुगतान की कुल अवधि {v} रहेगी।",
        "ऋण की मियाद {v} तय की गई है।",
        "आप {v} तक किश्तें भरेंगे।",
        "कुल {v} की अवधि के लिए यह लोन है।",
        "किश्तों की अवधि {v} है।",
        "यह ऋण {v} की अवधि का है।",
        "आपकी चुकौती अवधि {v} है।",
        "लोन {v} के लिए स्वीकृत है।",
    ],
    "emi_amount": [
        "आपकी हर महीने की किश्त {v} होगी।",
        "मासिक किश्त {v} बनती है।",
        "आपको हर महीने {v} जमा करने होंगे।",
        "आपकी ईएमआई {v} है।",
        "प्रति माह {v} की किश्त कटेगी।",
        "हर महीने की पहली तारीख़ को {v} कटेंगे।",
        "समान मासिक किश्त {v} तय हुई है।",
        "आपकी किश्त की रकम {v} है।",
        "मासिक भुगतान {v} रहेगा।",
        "हर किश्त {v} की होगी।",
    ],
    "account_identifier": [
        "आपके लोन खाते की संख्या {v} है।",
        "कृपया अपना खाता नंबर {v} जाँच लीजिए।",
        "यह ऋण खाता संख्या {v} पर दर्ज है।",
        "आपका खाता नंबर {v} है।",
        "भुगतान इसी खाता संख्या {v} में करना है।",
        "रिकॉर्ड में आपका खाता {v} दर्ज है।",
        "आपकी लोन आईडी {v} है।",
        "संदर्भ संख्या {v} नोट कर लीजिए।",
        "खाता संख्या {v} की पुष्टि कीजिए।",
        "आपके ऋण का खाता नंबर {v} है।",
    ],
    "date_deadline": [
        "आपकी पहली किश्त की तारीख़ {v} है।",
        "भुगतान की अंतिम तिथि {v} है।",
        "यह लोन {v} तक चुकाना ज़रूरी है।",
        "अगली किश्त {v} को देय है।",
        "ऋण की परिपक्वता तिथि {v} है।",
        "{v} तक आपको भुगतान करना होगा।",
        "समझौते की तारीख़ {v} है।",
        "आपकी आख़िरी किश्त {v} को होगी।",
        "देय तिथि {v} निर्धारित है।",
        "{v} को अगली राशि कटेगी।",
    ],
}


def group_indian(n: int) -> str:
    """1250000 -> '12,50,000'. Indian grouping is 2-2-3 from the right, not 3-3-3."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        head, part = head[:-2], head[-2:]
        parts.insert(0, part)
    return ",".join([head, *parts, tail]) if head else ",".join([*parts, tail])


def emi(principal: Decimal, annual_pct: Decimal, months: int) -> Decimal:
    """Standard annuity payment, rounded to the rupee."""
    r = annual_pct / Decimal(1200)
    growth = (1 + r) ** months
    return (principal * r * growth / (growth - 1)).quantize(Decimal("1"), ROUND_HALF_UP)


def apr(net: Decimal, payment: Decimal, months: int) -> Decimal:
    """Monthly IRR x 12. NOMINAL, not compounded — RBI's definition, on the NET amount.

    Bisection rather than a closed form: there isn't one, and 60 iterations is instant.
    """
    lo, hi = Decimal("0.000001"), Decimal("1")
    for _ in range(80):
        mid = (lo + hi) / 2
        growth = (1 + mid) ** months
        pv = payment * (growth - 1) / (mid * growth)
        lo, hi = (mid, hi) if pv > net else (lo, mid)
    return (lo * 1200).quantize(Decimal("0.01"), ROUND_HALF_UP)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31,
                                  30, 31, 30, 31][m - 1]))


def build_docs() -> list[KFS]:
    docs = []
    for i, (amount, rate, months, proc, ins, start) in enumerate(DOCS):
        principal, pct = Decimal(amount), Decimal(rate)
        fees = [FeeLine(item="प्रोसेसिंग शुल्क", amount_inr=Decimal(proc), net_of_gst=True)]
        if ins:
            fees.append(FeeLine(item="बीमा प्रीमियम", payable_to="third_party",
                                amount_inr=Decimal(ins)))
        pay = emi(principal, pct, months)
        net = principal - sum(f.amount_inr or Decimal(0) for f in fees)
        docs.append(KFS(
            proposal_no=f"SAMJHA-2026-{i + 1:03d}",
            loan_type="व्यक्तिगत ऋण",
            sanctioned_amount_inr=principal,
            loan_term_months=months,
            instalments=InstalmentDetails(number_of_epis=months, epi_amount_inr=pay,
                                          repayment_commencement="वितरण से 30 दिन बाद"),
            interest=InterestRate(pct=pct),
            fees=fees,
            apr_pct=apr(net, pay, months),
            total_repayment_inr=pay * months,
            cooling_off_period_days=3,
            loan_account_no=ACCOUNTS[i],
            grievance_officer_name="श्रीमती अनीता शर्मा",
            grievance_officer_phone="1800 200 3011",
        ))
    return docs


def _date_spoken(d: date) -> str:
    return f"{hindi.cardinal(d.day)} {MONTHS_HI[d.month]} {hindi.cardinal(d.year)}"


def build_items(docs: list[KFS]) -> list[dict]:
    """Twelve utterances per document, two per category. 120 total."""
    items: list[dict] = []
    counts: dict[str, int] = {}

    for i, doc in enumerate(docs):
        start = DOCS[i][5]
        slots = [
            ("rupee_amount", int(doc.sanctioned_amount_inr), "sanctioned_amount_inr"),
            ("rupee_amount", int(doc.total_repayment_inr), "total_repayment_inr"),
            ("percentage_apr", doc.apr_pct, "apr_pct"),
            ("percentage_apr", doc.interest.pct, "interest.pct"),
            ("tenure_months", doc.loan_term_months, "loan_term_months"),
            ("tenure_months", ALT_TENURES[i], "corpus (quoted alternate period)"),
            ("emi_amount", int(doc.instalments.epi_amount_inr), "instalments.epi_amount_inr"),
            ("emi_amount", int(doc.fees[0].amount_inr), "fees[0].amount_inr"),
            ("account_identifier", doc.loan_account_no, "loan_account_no"),
            ("account_identifier", ACCOUNTS[i + 10], "corpus (second identifier)"),
            ("date_deadline", add_months(start, 1), "first EPI due"),
            ("date_deadline", add_months(start, doc.loan_term_months), "final EPI due"),
        ]

        for kind, value, source in slots:
            n = counts[kind] = counts.get(kind, 0) + 1
            carriers = CARRIERS[kind]

            if kind in ("rupee_amount", "emi_amount"):
                raw, spoken, gt = f"₹{group_indian(value)}", hindi.rupees(value), value
            elif kind == "percentage_apr":
                # Trailing zeros stripped so the written and spoken forms agree digit for
                # digit: 21.10 and 21.1 are the same Decimal but not the same read-back.
                gt = str(value).rstrip("0").rstrip(".") if "." in str(value) else str(value)
                raw, spoken = f"{gt}%", hindi.percent(value)
            elif kind == "tenure_months":
                raw = f"{value} महीने"
                spoken, gt = hindi.tenure_months(value), value
            elif kind == "account_identifier":
                raw, spoken, gt = value, hindi.digits(value), value
            else:
                raw = f"{value.day} {MONTHS_HI[value.month]} {value.year}"
                spoken, gt = _date_spoken(value), value.isoformat()

            template = carriers[(n - 1 + i) % len(carriers)]
            items.append({
                "id": f"{kind}_{n:02d}",
                "type": kind,
                "value": gt,
                "intended_reading": "digit_by_digit" if kind == "account_identifier" else "cardinal",
                "raw_text": raw,
                "spoken_text": spoken,
                "carrier_template": template,
                "carrier_a": template.format(v=raw),
                "carrier_b": template.format(v=spoken),
                "doc_id": doc.proposal_no,
                "source": source,
            })

    return items


def main() -> None:
    docs = build_docs()
    doc_dir = HERE / "kfs_docs"
    doc_dir.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        (doc_dir / f"{doc.proposal_no}.json").write_text(
            doc.model_dump_json(indent=2), encoding="utf-8")

    items = build_items(docs)
    assert len(items) == 120, len(items)
    per_kind = {k: sum(1 for it in items if it["type"] == k) for k in
                {it["type"] for it in items}}
    assert set(per_kind.values()) == {20}, per_kind
    assert any(it["value"].startswith("0") for it in items
               if it["type"] == "account_identifier"), "leading-zero cases are the headline"

    out = HERE / "utterances.json"
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(docs)} KFS docs -> {doc_dir}")
    print(f"{len(items)} utterances -> {out}")
    for k in sorted(per_kind):
        ex = next(it for it in items if it["type"] == k)
        print(f"  {k:<20} {per_kind[k]:>3}   A: {ex['carrier_a']}")
        print(f"  {'':<20} {'':>3}   B: {ex['carrier_b']}")


if __name__ == "__main__":
    main()
