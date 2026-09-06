"""Score the returned listening sheets and compare them with the ASR scorers.

Human answers go through the SAME parser as ASR transcripts, so "1,25,000", "125000" and
"एक लाख पच्चीस हज़ार" all count as the same recovered value. A listener who writes what they
heard should never be marked wrong for their orthography.

**Where the panel and the ASR scorers disagree, the disagreement is reported, not resolved
in our favour** (ACCEPTANCE.md §7). The interesting cell is ASR-yes / human-no: that is
inverse-text-normalization reconstructing a value the listener never actually received.

    uv run python evals/human_panel/score_panel.py
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evals.asr_score import recovered  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def read_sheets() -> dict[tuple[str, str], str]:
    """(listener, clip) -> what they wrote. Comment rows and blanks are dropped."""
    out = {}
    for path in sorted((HERE / "responses").glob("panel_*.csv")):
        # csv quotes any header comment containing a comma, so a comment line can start
        # with `"#` rather than `#`. Missing that silently reads zero rows.
        for row in csv.DictReader(
                r for r in path.read_text(encoding="utf-8").splitlines()
                if not r.lstrip('"').startswith("#")):
            if row.get("clip"):
                out[(row["listener_id"], row["clip"])] = (row.get("heard_value") or "").strip()
    return out


def main() -> int:
    key_path = HERE / "key.json"
    if not key_path.exists():
        print("no key.json — run make_sheet.py first")
        return 1
    key = json.loads(key_path.read_text(encoding="utf-8"))
    by_clip = {c["clip"]: c for c in key["clips"]}

    answers = read_sheets()
    filled = {k: v for k, v in answers.items() if v}
    if not filled:
        print(f"{len(answers)} sheet rows found but every heard_value is blank.\n"
              "The panel has not been run yet. Nothing to score — this is not a failure,\n"
              "it is the honest state: evals/human_panel/panel_results.md stays unwritten\n"
              "until three non-author listeners have returned sheets.")
        return 2

    listeners = sorted({l for l, _ in filled})
    verdict: dict[str, dict[str, bool]] = defaultdict(dict)
    for (listener, clip), text in filled.items():
        verdict[clip][listener] = recovered(by_clip[clip], text)

    # ASR verdicts for the same clips, from the committed item-level evidence.
    asr: dict[tuple[str, str], bool] = {}
    il = ROOT / "evals/results/item_level.csv"
    if il.exists():
        for r in csv.DictReader(il.open(encoding="utf-8")):
            asr[(r["item_id"], r["arm"], r["scorer"])] = r["recovered"] == "True"

    arms = sorted({c["arm"] for c in key["clips"]})
    L = ["# Human panel — exploratory", "",
         f"{len(listeners)} listener(s), {len(by_clip)} clips, seed {key['seed']}. "
         "Blind to condition. Project authors excluded.", "",
         "## Recovery rate by listener and arm", ""]
    L.append("| listener | " + " | ".join(arms) + " |")
    L.append("|---|" + "---|" * len(arms))
    for listener in listeners:
        cells = []
        for arm in arms:
            sel = [v[listener] for c, v in verdict.items()
                   if by_clip[c]["arm"] == arm and listener in v]
            cells.append(f"{100 * sum(sel) / len(sel):.0f}% (n={len(sel)})" if sel else "—")
        L.append(f"| {listener} | " + " | ".join(cells) + " |")

    # Majority of the listeners who answered a given clip.
    majority = {c: sum(v.values()) * 2 > len(v) for c, v in verdict.items() if v}

    L += ["", "## Inter-listener agreement", ""]
    pairs = [(a, b) for i, a in enumerate(listeners) for b in listeners[i + 1:]]
    for a, b in pairs:
        both = [(v[a], v[b]) for v in verdict.values() if a in v and b in v]
        if both:
            L.append(f"- {a} vs {b}: {100 * sum(x == y for x, y in both) / len(both):.0f}% "
                     f"({len(both)} clips)")

    if asr:
        L += ["", "## Human majority vs ASR", "",
              "The cell that matters is **ASR yes / human no**: the provider's inverse text "
              "normalization rebuilt a value the listener never received.", ""]
        L.append("| scorer | agree | ASR yes / human no | ASR no / human yes | n |")
        L.append("|---|---|---|---|---|")
        for scorer in sorted({k[2] for k in asr}):
            cells = [(asr[(by_clip[c]["item_id"], by_clip[c]["arm"], scorer)], m)
                     for c, m in majority.items()
                     if (by_clip[c]["item_id"], by_clip[c]["arm"], scorer) in asr]
            if not cells:
                continue
            L.append(f"| {scorer} | {sum(a == h for a, h in cells)} "
                     f"| {sum(a and not h for a, h in cells)} "
                     f"| {sum(h and not a for a, h in cells)} | {len(cells)} |")

        disagreements = [
            (c, by_clip[c], scorer) for c in majority for scorer in sorted({k[2] for k in asr})
            if (by_clip[c]["item_id"], by_clip[c]["arm"], scorer) in asr
            and asr[(by_clip[c]["item_id"], by_clip[c]["arm"], scorer)] != majority[c]
        ]
        if disagreements:
            L += ["", "### Every disagreement, itemised", ""]
            for c, meta, scorer in sorted(disagreements):
                L.append(f"- `{c}` {meta['item_id']} ({meta['arm']}, {meta['type']}, "
                         f"value `{meta['value']}`) — {scorer} said "
                         f"{asr[(meta['item_id'], meta['arm'], scorer)]}, "
                         f"human majority said {majority[c]}")

    L += ["", "## Limitations", "",
          f"- {len(listeners)} listeners, {len(by_clip)} clips. **Exploratory. Not a benchmark.**",
          "- One accent register, one voice (Coda has exactly two Hindi voices).",
          "- Listeners heard each clip once, which is harsher than a real call where a "
          "borrower can ask again — and gentler than one where she cannot.", ""]

    out = HERE / "panel_results.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
