"""Generate the blind listening panel: 30 clips, randomized, condition stripped.

The panel exists because ASR is a proxy for human comprehension and our own day-1 pilot
already caught the proxy failing: `9157114007` scored as RECOVERED because Sarvam's inverse
text normalization reconstructed digits from a spoken *quantity* — the borrower heard
"nine billion fifteen crore..." and could not verify anything. ASR said yes; a human would
say no. So this is load-bearing, not confirmatory (ACCEPTANCE.md §7, amendment 1).

Blinding, concretely:
  * filenames are `clip_01.wav`; nothing in them encodes arm, category or value;
  * order is shuffled with a recorded seed, so both arms are interleaved;
  * the mapping lives in `key.json`, which listeners never receive;
  * PROJECT AUTHORS ARE EXCLUDED. An author who knows which arm is which is not a blind
    listener, and including one would quietly convert the panel into a rubber stamp.

    uv run python evals/human_panel/make_sheet.py            # after an eval run
    uv run python evals/human_panel/make_sheet.py --n 30 --seed 20260906
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
ITEM_LEVEL = ROOT / "evals/results/item_level.csv"
CORPUS = ROOT / "evals/corpus/utterances.json"

LISTENERS = ("L1", "L2", "L3")

INSTRUCTIONS = [
    "Listen to each clip ONCE, at normal volume, over headphones or a phone speaker.",
    "Write down the NUMBER you heard, exactly as you heard it. Digits or Hindi words —",
    "  whichever is closer to what was said. If you did not catch it, leave it blank.",
    "Do not replay a clip. Do not guess from context. A blank is a real answer.",
    "confidence: 1 = not sure at all, 5 = certain.",
    "",
    "सुनिए और जो संख्या सुनाई दी वही लिखिए। अगर समझ न आए तो खाली छोड़ दीजिए।",
]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--seed", type=int, default=20260906, help="recorded, so the draw is reproducible")
    args = p.parse_args()

    if not ITEM_LEVEL.exists():
        print(f"{ITEM_LEVEL} not found — run `make eval` first.")
        return 1

    corpus = {it["id"]: it for it in json.loads(CORPUS.read_text(encoding="utf-8"))}
    rows = list(csv.DictReader(ITEM_LEVEL.open(encoding="utf-8")))
    # One entry per (item, arm): scorers duplicate rows and we are sampling CLIPS.
    clips = {(r["item_id"], r["arm"]): r for r in rows}

    # Balanced across (category, arm) so the panel can speak to the per-category table and
    # not just to the headline. account_identifier is the primary claim, so it is not
    # allowed to be crowded out by the four negative-result categories.
    buckets: dict[tuple[str, str], list] = defaultdict(list)
    for (item_id, arm), r in clips.items():
        buckets[(r["type"], arm)].append(r)

    rng = random.Random(args.seed)
    for v in buckets.values():
        rng.shuffle(v)

    order = sorted(buckets, key=lambda k: (k[0] != "account_identifier", k))
    picked, i = [], 0
    while len(picked) < args.n and any(buckets[k] for k in order):
        for k in order:
            if buckets[k] and len(picked) < args.n:
                picked.append(buckets[k].pop())
        i += 1
    if len(picked) < args.n:
        print(f"only {len(picked)} clips available; run a larger eval for a full panel")

    rng.shuffle(picked)

    clip_dir = HERE / "clips"
    if clip_dir.exists():
        shutil.rmtree(clip_dir)
    clip_dir.mkdir(parents=True)

    key = []
    for n, r in enumerate(picked, 1):
        name = f"clip_{n:02d}.wav"
        shutil.copyfile(ROOT / r["clip"], clip_dir / name)
        key.append({"clip": name, "item_id": r["item_id"], "arm": r["arm"],
                    "type": r["type"], "value": corpus[r["item_id"]]["value"],
                    "text_sent": r["text_sent"]})

    (HERE / "key.json").write_text(json.dumps(
        {"seed": args.seed, "n": len(key),
         "authors_excluded": True,
         "note": "NOT FOR LISTENERS. Reveals the condition.",
         "clips": key}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    resp = HERE / "responses"
    resp.mkdir(exist_ok=True)
    for listener in LISTENERS:
        path = resp / f"panel_{listener}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["# blind listening sheet — SAMJHA. Project authors must not fill this in."])
            for line in INSTRUCTIONS:
                w.writerow([f"# {line}"])
            w.writerow(["listener_id", "clip", "heard_value", "confidence_1_5"])
            for row in key:
                w.writerow([listener, row["clip"], "", ""])
        print(f"  {path.relative_to(ROOT)}")

    print(f"\n{len(key)} clips -> {clip_dir.relative_to(ROOT)} (seed {args.seed})")
    print(f"key (authors only) -> {(HERE / 'key.json').relative_to(ROOT)}")
    print("Give listeners the clips/ folder and ONE sheet each. Never key.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
