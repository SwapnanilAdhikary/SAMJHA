# SAMJHA eval — 20260906T131218Z

120 utterances x 2 arms x 1 scorer(s) = 240 scored rows.  FULL run.

Clips: 0 fresh, 240 cached (cached and uncached reported separately, as the checklist requires). ASR: 240 cached, 0 fresh.

## Value Error Rate — overall

Lower is better. VER = % of comprehension-critical values NOT recovered exactly.

| scorer | baseline | samjha | delta |
|---|---|---|---|
| deepgram | 17.5% | 5.8% | +11.7 pp |

## Value Error Rate — per category

**This table is the deliverable.** The pre-registered claim was narrowed on day 1 to digit-sequence identifiers; the other four categories are reported as the negative result they are, not dropped.

| category | intended reading | scorer | baseline | samjha | delta | n |
|---|---|---|---|---|---|---|
| account_identifier | digit_by_digit | deepgram | 100.0% | 20.0% | +80.0 pp | 20 |
| date_deadline | cardinal | deepgram | 5.0% | 0.0% | +5.0 pp | 20 |
| emi_amount | cardinal | deepgram | 0.0% | 5.0% | -5.0 pp | 20 |
| percentage_apr | cardinal | deepgram | 0.0% | 5.0% | -5.0 pp | 20 |
| rupee_amount | cardinal | deepgram | 0.0% | 5.0% | -5.0 pp | 20 |
| tenure_months | cardinal | deepgram | 0.0% | 0.0% | +0.0 pp | 20 |

## Reproduce

```
make preflight
make eval        # smoke
uv run python evals/run_eval.py --full
```

Config, versions and endpoint: `results/run_config.json`. Per-row evidence: `results/item_level.csv`. Limitations: `evals/ACCEPTANCE.md` §8, written before the result was known.
