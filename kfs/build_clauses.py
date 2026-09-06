"""KFS -> the ordered list of Hindi clauses the agent reads aloud.

The one rule that matters: **a Segment carries AT MOST ONE key value.** Everything else
here follows from it. It is not a style preference — it is what makes "did she hear the
APR?" reduce to "did segment k finish playing?", which is exact byte arithmetic instead of
a guess. Rime emits no word timestamps for Hindi (measured, see
evals/results/battery/FINDINGS.md), so there is no other honest way to answer it.

Consequences you will notice reading the code:

  * Sentences are short, and a value gets its own sentence even when Hindi would happily
    carry two. That costs a little naturalness and buys the entire consent mechanic.
  * Every value is rendered by `delivery.normalizer.hindi`, including the categories the
    day-1 pilot showed Rime already handles correctly raw (rupees, percentages, tenures).
    Routing them all through one path keeps the A/B arms differing in exactly one thing.
  * Nothing in a segment is Latin script. Romanized input is measurably worse — the battery
    clocked a romanized sentence at 2.1x the duration of its Devanagari twin, and one
    romanized numeral came back as a DIFFERENT VALUE (chaudah/14 -> सोलह/16). Two fields in
    the schema are free English text (`grievance_officer_name`, `benchmark`); they are
    deliberately not spoken, and what replaces them is noted at the call site.

Clause order follows Annex A / `kfs.schema.KFS` field order, which is the order a lender is
required to disclose in.

Self-check:  uv run python -m kfs.build_clauses
"""

from __future__ import annotations

from decimal import Decimal

from delivery.normalizer.hindi import cardinal, digits, percent, rupees, tenure_months
from kfs.clauses import Clause, KeyValue, Segment
from kfs.schema import KFS, FeeLine

LOAN_TYPE_HI = {
    "Two-Wheeler Loan": "दोपहिया वाहन लोन",
    "Personal Loan": "पर्सनल लोन",
    "Consumer Durable Loan": "घरेलू सामान के लोन",
    "Gold Loan": "गोल्ड लोन",
    "Small Business Loan": "छोटे कारोबार के लोन",
}

FEE_ITEM_HI = {
    "Processing fee": "प्रोसेसिंग फीस",
    "Documentation charges": "कागज़ी कार्रवाई का शुल्क",
    "Vehicle insurance premium": "गाड़ी के बीमे का प्रीमियम",
    "Foreclosure charges": "लोन जल्दी बंद करने का शुल्क",
    "Credit protect premium": "क्रेडिट बीमे का प्रीमियम",
    "Annual service charge": "सर्विस चार्ज",
    "Part-prepayment charges": "कुछ रकम पहले चुकाने का शुल्क",
    "Dealer subvention recovery": "डीलर छूट की वसूली",
    "Gold valuation charges": "सोने की जाँच का शुल्क",
    "Auction notice charges": "नीलामी नोटिस का शुल्क",
    "Stamp duty": "स्टाम्प ड्यूटी",
    "Annual renewal fee": "रिन्यूअल फीस",
    "Late payment charges": "देरी से भुगतान का शुल्क",
    "RTO / hypothecation charges": "आर टी ओ और हाइपोथिकेशन का शुल्क",
    "Extended warranty premium": "एक्सटेंडेड वारंटी का प्रीमियम",
    "Cheque bounce charges": "चेक बाउंस का शुल्क",
}

# Annex A row 5. A standardised form has a small controlled vocabulary here; anything
# outside it falls back to telling the borrower where to look, which is true for any
# document, rather than reading an unmapped English string into a Hindi voice.
COMMENCEMENT_HI = {
    "30 days from disbursal": "पैसा मिलने के तीस दिन बाद",
    "45 days from first disbursal": "पहला पैसा मिलने के पैंतालीस दिन बाद",
    "1st of the month following disbursal": "पैसा मिलने के अगले महीने की पहली तारीख़ को",
}

PAYEE_HI = {"RE": "लोन कंपनी को", "third_party": "किसी दूसरी कंपनी को"}


def build_clauses(kfs: KFS) -> list[Clause]:
    """The full read-aloud script for one KFS, in disclosure order."""
    out = [
        _identity(kfs),
        _sanctioned(kfs),
        _tenure(kfs),
        _emi(kfs),
        _interest_rate(kfs),
    ]
    if kfs.floating is not None:
        out.append(_rate_reset(kfs))
    out += [
        _fees(kfs),
        _apr(kfs),
        _total_repayment(kfs),
        _prepayment(kfs),
        _grievance(kfs),
    ]
    return out


# --- clauses -------------------------------------------------------------------------


def _identity(kfs: KFS) -> Clause:
    acct = kfs.loan_account_no
    return Clause(
        id="identity",
        title_hi="लोन की पहचान",
        segments=[
            Segment(f"नमस्ते। यह आपके {_loan_type(kfs)} की ज़रूरी जानकारी है।"),
            Segment("मैं एक-एक बात पढ़कर सुनाऊँगा। बीच में कभी भी रोक सकते हैं।"),
            Segment(
                f"आपका लोन खाता नंबर है, {digits(acct)}।",
                # str, never int: the pilot's worst raw failure was a LOST LEADING ZERO
                # (000512348899 -> 95058000000). int() would lose it here too.
                key_value=KeyValue("account_identifier", acct, acct, digits(acct)),
            ),
        ],
    )


def _sanctioned(kfs: KFS) -> Clause:
    amt = kfs.sanctioned_amount_inr
    staged = kfs.disbursal_mode == "stages"
    return Clause(
        id="sanctioned",
        title_hi="मंज़ूर रकम",
        segments=[
            Segment(
                f"आपको मंज़ूर हुई रकम है, {rupees(amt)}।",
                key_value=KeyValue("rupee_amount", amt, f"₹{inr(amt)}", rupees(amt)),
            ),
            Segment(
                "यह रकम एक साथ नहीं, किस्तों में दी जाएगी।"
                if staged
                else "यह पूरी रकम एक ही बार में आपके खाते में आएगी।"
            ),
        ],
    )


def _tenure(kfs: KFS) -> Clause:
    m = kfs.loan_term_months
    return Clause(
        id="tenure",
        title_hi="लोन की अवधि",
        segments=[
            Segment(
                f"आपके लोन की अवधि है, {tenure_months(m)}।",
                key_value=KeyValue("tenure_months", m, f"{m} months", tenure_months(m)),
            ),
        ],
    )


def _emi(kfs: KFS) -> Clause:
    inst = kfs.instalments
    epi, n = inst.epi_amount_inr, inst.number_of_epis
    start = COMMENCEMENT_HI.get(
        inst.repayment_commencement,
        "पहली किस्त की तारीख़ आपके लोन दस्तावेज़ में लिखी है",
    )
    return Clause(
        id="emi",
        title_hi="महीने की किस्त",
        segments=[
            Segment(
                f"हर महीने की किस्त होगी, {rupees(epi)}।",
                key_value=KeyValue("emi_amount", epi, f"₹{inr(epi)}", rupees(epi)),
            ),
            Segment(
                f"कुल {cardinal(n)} किस्तें देनी होंगी।",
                key_value=KeyValue("tenure_months", n, str(n), cardinal(n)),
            ),
            Segment(f"पहली किस्त {start}।"),
        ],
    )


def _interest_rate(kfs: KFS) -> Clause:
    pct = kfs.interest.pct
    floating = kfs.interest.kind != "fixed"
    return Clause(
        id="interest_rate",
        title_hi="ब्याज दर",
        segments=[
            Segment(
                f"आपके लोन पर ब्याज दर है, {percent(pct)}।",
                key_value=KeyValue("percentage_apr", pct, f"{pct}%", percent(pct)),
            ),
            Segment(
                "यह दर घट-बढ़ सकती है।"
                if floating
                else "यह दर पूरे समय एक जैसी रहेगी। यह बदलेगी नहीं।"
            ),
        ],
    )


def _rate_reset(kfs: KFS) -> Clause:
    """Annex A row 10. The awkward clause, and the one a borrower most needs read aloud.

    The benchmark's own name (`floating.benchmark`, e.g. "Lender's 6-month MCLR") is NOT
    spoken: it is Latin-script free text, and romanized input into a Hindi voice measured
    2.1x longer and changed one numeral's value in the battery. The borrower gets the two
    things she can act on — the spread and the reset period — plus the two disclosed
    impacts of a 25 bps move.
    """
    f = kfs.floating
    assert f is not None
    segs = [
        Segment("आपकी ब्याज दर तय नहीं है। यह कंपनी की बेंचमार्क दर से जुड़ी है।"),
        Segment(
            f"उस दर के ऊपर, {percent(f.spread_pct)} और जोड़ा जाता है।",
            key_value=KeyValue(
                "percentage_apr", f.spread_pct, f"{f.spread_pct}%", percent(f.spread_pct)
            ),
        ),
        Segment(
            f"हर {tenure_months(f.reset_periodicity_months, confirm=False)} में दर की समीक्षा होगी।",
            key_value=KeyValue(
                "tenure_months",
                f.reset_periodicity_months,
                f"{f.reset_periodicity_months} months",
                tenure_months(f.reset_periodicity_months, confirm=False),
            ),
        ),
    ]
    if f.impact_25bps_on_epi is not None:
        d = f.impact_25bps_on_epi
        segs.append(
            Segment(
                f"अगर दर पाव प्रतिशत बढ़ी, तो किस्त {rupees(d)} बढ़ जाएगी।",
                key_value=KeyValue("emi_amount", d, f"₹{inr(d)}", rupees(d)),
            )
        )
    if f.impact_25bps_on_num_epis is not None:
        n = f.impact_25bps_on_num_epis
        segs.append(
            Segment(
                # Phrased in months, not instalments, so it stays grammatical at n = 1.
                f"या किस्त उतनी ही रखकर, लोन {cardinal(n)} महीने और चलेगा।",
                key_value=KeyValue("tenure_months", n, str(n), cardinal(n)),
            )
        )
    return Clause(id="rate_reset", title_hi="दर बदलने का नियम", segments=segs)


def _fees(kfs: KFS) -> Clause:
    segs = [Segment("अब फीस और शुल्क। ये रकम लोन के अलावा है।")]
    for fee in kfs.fees:
        segs.append(_fee_segment(fee))
    if not kfs.fees:
        segs.append(Segment("इस लोन पर कोई अलग फीस नहीं है।"))
    return Clause(id="fees", title_hi="फीस और शुल्क", segments=segs)


def _fee_segment(fee: FeeLine) -> Segment:
    # An unmapped item reads in English inside a Hindi sentence. That is what a lender's
    # own agent does on a real call, and PLAN.md treats code-switching as real rather than
    # something to design away.
    item = FEE_ITEM_HI.get(fee.item, fee.item)
    when = "हर साल " if fee.timing == "recurring" else ""
    payee = PAYEE_HI[fee.payable_to]

    if fee.amount_inr is not None:
        text = f"{when}{item}, {rupees(fee.amount_inr)}, {payee} देनी होगी।"
        # Annex A footnote 4 permits disclosure net of taxes. A borrower who hears
        # Rs 1,180 and is debited Rs 1,392 was misinformed even though the lender complied.
        if fee.net_of_gst:
            text += " यह रकम जी एस टी के बिना है।"
        return Segment(
            text,
            key_value=KeyValue(
                "rupee_amount", fee.amount_inr, f"₹{inr(fee.amount_inr)}", rupees(fee.amount_inr)
            ),
        )

    pct = fee.percentage or Decimal(0)
    return Segment(
        f"{when}{item}, {percent(pct)}।",
        key_value=KeyValue("percentage_apr", pct, f"{pct}%", percent(pct)),
    )


def _apr(kfs: KFS) -> Clause:
    apr = kfs.apr_pct
    return Clause(
        id="apr",
        title_hi="सालाना कुल दर",
        segments=[
            Segment("अब सबसे ज़रूरी बात। फीस मिलाकर आपकी असली सालाना दर।"),
            Segment(
                f"वह दर है, {percent(apr)}।",
                key_value=KeyValue("percentage_apr", apr, f"{apr}%", percent(apr)),
            ),
            Segment("यह ब्याज दर से ज़्यादा है, क्योंकि इसमें फीस भी गिनी जाती है।"),
        ],
    )


def _total_repayment(kfs: KFS) -> Clause:
    total = kfs.total_repayment_inr
    return Clause(
        id="total_repayment",
        title_hi="कुल चुकाने की रकम",
        segments=[
            Segment(
                f"सब मिलाकर आपको कुल {rupees(total)} चुकाने होंगे।",
                key_value=KeyValue("rupee_amount", total, f"₹{inr(total)}", rupees(total)),
            ),
            Segment("इसमें मूल रकम, ब्याज और फीस, सब शामिल है।"),
        ],
    )


def _prepayment(kfs: KFS) -> Clause:
    segs = [Segment("आप लोन तय समय से पहले भी बंद कर सकते हैं।")]
    days = kfs.cooling_off_period_days
    if days:
        # `date_deadline` is the corpus category for a time-bounded right. It is a count of
        # days here, not a calendar date; the typed value is the day count.
        segs.append(
            Segment(
                f"पैसा मिलने के {cardinal(days)} दिन के अंदर आप बिना जुर्माने के लोन लौटा सकते हैं।",
                key_value=KeyValue("date_deadline", days, f"{days} days", cardinal(days)),
            )
        )
        segs.append(Segment("उसके बाद जो शुल्क लगेगा, वह फीस वाली सूची में बताया गया है।"))
    else:
        segs.append(Segment("इस लोन में कूलिंग-ऑफ की अवधि नहीं है।"))
    return Clause(id="prepayment", title_hi="लोन जल्दी बंद करना", segments=segs)


def _grievance(kfs: KFS) -> Clause:
    phone = kfs.grievance_officer_phone
    # The officer's NAME is not spoken: `grievance_officer_name` is Latin-script free text
    # and this is a Hindi voice. The number is the part she can act on, and it is a
    # digit-sequence identifier — the one category the pilot proved needs the normaliser.
    return Clause(
        id="grievance",
        title_hi="शिकायत",
        segments=[
            Segment("अगर कोई शिकायत हो, तो कंपनी के शिकायत अधिकारी को फ़ोन कीजिए।"),
            Segment(
                f"उनका नंबर है, {digits(phone)}।",
                key_value=KeyValue("account_identifier", phone, phone, digits(phone)),
            ),
            Segment("वहाँ बात न बने, तो आप आर बी आई के लोकपाल से भी शिकायत कर सकते हैं।"),
        ],
    )


# --- helpers -------------------------------------------------------------------------


def _loan_type(kfs: KFS) -> str:
    return LOAN_TYPE_HI.get(kfs.loan_type, kfs.loan_type)


def inr(amount: Decimal) -> str:
    """Indian 2-2-3 grouping, no currency symbol. 104596 -> '1,04,596'.

    Feeds `KeyValue.raw_text` (with a ₹ prefixed) and the printed KFS PDF (without one —
    reportlab's Helvetica has no U+20B9 glyph). Never sent to the TTS in either case.
    """
    whole, _, frac = f"{amount:f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    parts = []
    while len(head) > 2:
        head, chunk = head[:-2], head[-2:]
        parts.insert(0, chunk)
    if head:
        parts.insert(0, head)
    grouped = ",".join(parts + [tail]) if parts else tail
    return grouped + (f".{frac}" if frac and frac.rstrip("0") else "")


def _demo() -> None:
    """Read every fixture, print the awkward one, and assert the invariants."""
    import json
    from pathlib import Path

    fixtures = sorted((Path(__file__).parent.parent / "fixtures/synthetic").glob("*.json"))
    assert fixtures, "no fixtures — run PYTHONPATH=. uv run python fixtures/synthetic/_generate.py"

    for path in fixtures:
        doc = KFS.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for clause in build_clauses(doc):
            assert clause.segments, f"{path.name}/{clause.id} has no segments"
            for seg in clause.segments:
                assert len(seg.text) < 1000, f"{path.name}/{clause.id}: over Rime's cap"

    doc = KFS.model_validate(
        json.loads((fixtures[4]).read_text(encoding="utf-8"))
    )  # the floating-rate one
    for clause in build_clauses(doc):
        print(f"\n[{clause.id}] {clause.title_hi}")
        for seg in clause.segments:
            mark = f"  <- {seg.key_value.kind}={seg.key_value.value!r}" if seg.key_value else ""
            print(f"    {seg.text}{mark}")

    print(f"\n{len(fixtures)} fixtures built; invariants hold")


if __name__ == "__main__":
    _demo()
