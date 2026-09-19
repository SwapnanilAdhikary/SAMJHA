"""The BORROWER's lines: what to say back when the agent asks, for any KFS.

    make answers                              # demo/demo_kfs.pdf
    make answers ARGS="--doc fixtures/synthetic/pdf/kfs_01_two_wheeler.pdf"

TALK_SCRIPT.md is the same idea hand-written against one fixture. This is generated from
whatever document you are actually demoing, because the numbers you have to say back come
out of that document and nowhere else — a card that names last week's account number is
worse than no card.

Every line printed here is checked against `agent.teachback.grade_offline` before it is
shown, so a line you read aloud is one the grader has already accepted. The offline grader
is the strict one; passing it means the LLM grader passes too.

Answers are templates per clause id with the document's own spoken forms dropped in, so
they follow the document. An unknown clause id falls back to naming its values, which
grades correctly even if it reads stiffly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.teachback import facts_for, grade_offline  # noqa: E402
from kfs.build_clauses import build_clauses  # noqa: E402
from kfs.extract import extract  # noqa: E402

G, Y, R, B, D, X = ("\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[2m", "\033[0m")

# {0}, {1}… are the clause's key values in order, in their SPOKEN form.
TEMPLATES = {
    "identity": "मेरा खाता नंबर {0} है।",
    "sanctioned": "मुझे {0} मिलेंगे।",
    "tenure": "{0}।",
    "emi": "हर महीने की किस्त {0} है।",
    "emi_count": "कुल {0} किस्तें देनी होंगी।",
    "interest_rate": "ब्याज दर {0} है।",
    "rate_reset": "बेंचमार्क के ऊपर {0} और जुड़ता है।",
    "rate_reset_period": "हर {0} में दर की समीक्षा होगी।",
    "rate_reset_emi": "किस्त {0} बढ़ जाएगी।",
    "rate_reset_term": "लोन {0} महीने और चलेगा।",
    "fees": "इस लोन पर कोई अलग फीस नहीं है।",
    "apr": "सालाना कुल दर {0} है।",
    "total_repayment": "कुल {0} चुकाने होंगे।",
    "prepayment": "{0} दिन के अंदर बिना जुर्माने के बंद कर सकते हैं।",
    "grievance": "शिकायत का नंबर {0} है।",
}


def answer_for(clause) -> str:
    spoken = [kv.spoken_text for kv in clause.key_values]
    # fee_1, fee_2, ... are generated per fee line, so they cannot be keyed literally.
    # The clause title IS the fee's own name, which makes a natural sentence.
    if clause.id.startswith("fee_") and spoken:
        return f"{clause.title_hi} {spoken[0]} है।"
    tmpl = TEMPLATES.get(clause.id)
    if tmpl is None or tmpl.count("{") != len(spoken):
        # Naming every value is always gradeable, even when it reads stiffly.
        return ", ".join(spoken) + "।"
    return tmpl.format(*spoken)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="demo/demo_kfs.pdf")
    args = ap.parse_args()

    doc = Path(args.doc)
    kfs = extract(doc)
    clauses = build_clauses(kfs)

    print(f"\n{B}BORROWER'S CARD{X}  {doc}")
    print(f"{D}loan {kfs.loan_account_no} · every line below is pre-checked against the "
          f"teach-back grader{X}")
    print(f"{D}ENTER while it speaks = interrupt · answer out loud when it asks{X}\n")

    failed = 0
    for i, c in enumerate(clauses, 1):
        facts = facts_for(c)
        ans = answer_for(c)
        print(f"{B}{i}. {c.title_hi}{X}  {D}({c.id}){X}")
        print(f"   {D}it asks:{X} {c.title_hi} के बारे में आपने क्या समझा?")

        if not facts:
            print(f"   {D}no key values — this clause is not graded, say anything{X}\n")
            continue

        r = grade_offline(facts, ans)
        if r.passed:
            print(f"   {G}YOU SAY:{X} {ans}")
        else:
            failed += 1
            print(f"   {R}YOU SAY:{X} {ans}   {R}<-- GRADER REJECTS THIS{X}")
            for g in r.grades:
                if g.verdict != "understood":
                    print(f"      {R}{g.fact_id}: {g.verdict}{X} {g.reason}")
        for kv in c.key_values:
            print(f"   {D}   must convey: {kv.raw_text}{X}")
        print()

    print(f"{B}consent{X}")
    print(f"   {D}it asks:{X} क्या आप इन शर्तों पर सहमत हैं?")
    print(f"   {G}YOU SAY:{X} हाँ, मैं सहमत हूँ।")
    print(f"   {D}   wait ~2s first — an instant yes is refused as a reflex{X}\n")

    if failed:
        print(f"{R}{B}{failed} line(s) the grader rejects{X} — fix TEMPLATES in this file.")
        return 1
    print(f"{G}{B}all {len(clauses)} lines accepted by the offline grader{X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
