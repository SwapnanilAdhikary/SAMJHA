# Human panel — protocol

Exploratory. 3 native Hindi speakers, 30 clips, blind to condition.

## Why it is not decoration

ASR is a proxy for human comprehension, and our own day-1 pilot caught the proxy failing.
`9157114007` was scored **recovered** on the raw arm because Sarvam's inverse text
normalization rebuilt the digits from a spoken *quantity*: what was actually said was
*नौ अरब पंद्रह करोड़ इकहत्तर लाख चौदह हज़ार सात* — "nine billion fifteen crore…". A borrower
hearing that cannot verify her account number. **By ASR the raw arm scored 1/5; by human
comprehension it is plausibly 0/5.**

That is the SP-MCQA failure mode (arXiv:2510.26190) showing up in our own data on day 1,
and it is why ACCEPTANCE.md amendment 1 promoted this panel from confirmatory to
load-bearing.

## Protocol

1. `uv run python evals/human_panel/make_sheet.py` — after an eval run. It draws 30 clips
   balanced across (category, arm), shuffles them with a recorded seed, copies them out as
   `clip_01.wav` … with **no condition, category or value in the filename**, and writes one
   sheet per listener under `responses/`.
2. Give each listener the `clips/` folder and **their own sheet only**. Never `key.json` —
   it reveals the arm.
3. Each clip is heard **once**, at normal volume. The listener writes the number they heard,
   in digits or Hindi words, whichever is closer to what was said, plus a 1–5 confidence.
   A blank is a real answer and must not be filled in later.
4. `uv run python evals/human_panel/score_panel.py` — parses the answers with the same
   parser used on ASR transcripts, so a listener is never marked wrong for orthography, and
   writes `panel_results.md`.

## Author exclusion

**Project authors do not listen.** An author knows which arm is which and can reconstruct
the ground truth from memory of the corpus; including one converts the panel into a rubber
stamp. This is stated in ACCEPTANCE.md §7 and is not negotiable for a result we report.

## What gets reported

Per-listener recovery by arm, pairwise inter-listener agreement, and the human-majority vs
ASR confusion — including every individual disagreement. The cell that matters is
**ASR yes / human no**. Disagreements are reported, never resolved in our favour.

## Limitations

3 listeners, 30 clips, one voice (Coda exposes exactly two Hindi voices), one accent
register, one noise profile. Exploratory, not a benchmark.
