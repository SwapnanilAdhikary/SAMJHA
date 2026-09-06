# SAMJHA eval — 20260906T133206Z

24 utterances x 2 arms x 2 scorer(s) = 96 scored rows.  SMOKE run.

Clips: 0 fresh, 96 cached (cached and uncached reported separately, as the checklist requires). ASR: 72 cached, 24 fresh.

## Value Error Rate — overall

Lower is better. VER = % of comprehension-critical values NOT recovered exactly.

| scorer | baseline | samjha | delta |
|---|---|---|---|
| deepgram | 16.7% | 4.2% | +12.5 pp |
| sarvam | 12.5% | 0.0% | +12.5 pp |

## Value Error Rate — per category

**This table is the deliverable.** The pre-registered claim was narrowed on day 1 to digit-sequence identifiers; the other four categories are reported as the negative result they are, not dropped.

| category | intended reading | scorer | baseline | samjha | delta | n |
|---|---|---|---|---|---|---|
| account_identifier | digit_by_digit | deepgram | 100.0% | 0.0% | +100.0 pp | 4 |
| account_identifier | digit_by_digit | sarvam | 75.0% | 0.0% | +75.0 pp | 4 |
| date_deadline | cardinal | deepgram | 0.0% | 0.0% | +0.0 pp | 4 |
| date_deadline | cardinal | sarvam | 0.0% | 0.0% | +0.0 pp | 4 |
| emi_amount | cardinal | deepgram | 0.0% | 0.0% | +0.0 pp | 4 |
| emi_amount | cardinal | sarvam | 0.0% | 0.0% | +0.0 pp | 4 |
| percentage_apr | cardinal | deepgram | 0.0% | 0.0% | +0.0 pp | 4 |
| percentage_apr | cardinal | sarvam | 0.0% | 0.0% | +0.0 pp | 4 |
| rupee_amount | cardinal | deepgram | 0.0% | 25.0% | -25.0 pp | 4 |
| rupee_amount | cardinal | sarvam | 0.0% | 0.0% | +0.0 pp | 4 |
| tenure_months | cardinal | deepgram | 0.0% | 0.0% | +0.0 pp | 4 |
| tenure_months | cardinal | sarvam | 0.0% | 0.0% | +0.0 pp | 4 |

## Inter-scorer agreement

Two scorers that agree on almost everything are one measurement. Reported instead of quietly taking the better number.

- pairs compared: 48
- observed agreement: 95.8%
- Cohen's kappa: 0.729
- recovery rate — sarvam 93.8%, deepgram 89.6%

## The inverse-text-normalization artifact

A digit-sequence value that matches ONLY on Sarvam's `transcribe` pass may have been spoken as a *quantity* and reconstructed into digits by the provider's ITN. The borrower heard `नौ अरब पंद्रह करोड़ ...` and could not verify anything. By ASR that scores as recovered; by comprehension it is not. This is why the human panel is load-bearing (ACCEPTANCE.md §7, amendment 1).

| arm | matched on transcribe only | matched on verbatim | not recovered |
|---|---|---|---|
| baseline | 1 | 0 | 3 |
| samjha | 4 | 0 | 0 |

## Reproduce

```
make preflight
make eval        # smoke
uv run python evals/run_eval.py --full
```

Config, versions and endpoint: `results/run_config.json`. Per-row evidence: `results/item_level.csv`. Limitations: `evals/ACCEPTANCE.md` §8, written before the result was known.
