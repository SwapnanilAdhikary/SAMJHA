"""`make eval` — the A/B that produces every number in the README.

For each utterance, both arms are synthesized by Rime with IDENTICAL request parameters,
pushed through the same simulated telephone channel, transcribed by both scorers and scored
on parsed typed values.

    A — baseline  the clause as a KFS writes it            (utterance["carrier_a"])
    B — samjha    the same clause, values verbalised by     (utterance["carrier_b"])
                  delivery/normalizer/hindi.py

⚠️ ONE DELIBERATE DEVIATION FROM ACCEPTANCE.md, disclosed rather than silently taken.
§4 requires "chunk boundaries identical across arms" in its held-constant table AND
describes arm B as "normalizer -> chunker". Those cannot both hold. We took the stricter
reading — §4's own opening line, "exactly one thing differs between arms: the text sent to
Rime" — so both arms are one flush segment per carrier sentence and the measured effect is
attributable to normalization alone. That is the conservative choice: it can only
UNDERSTATE the delivery layer, never inflate it. Each carrier already contains exactly one
key value, so the one-value-per-segment property holds in both arms by construction.

COST GUARD. Sarvam's free credit is about Rs 100, roughly 66 minutes of audio, and every
clip is sent to Sarvam TWICE (transcribe + verbatim). A full 120x2 run is ~40 minutes of
Sarvam audio. So the default is a small stratified smoke run and the real thing needs
--full. Audio and transcripts are both cached on disk, keyed by content, so a re-run costs
nothing; cached and fresh counts are reported separately.

    uv run python evals/run_eval.py                    # smoke: 6 items, both arms
    uv run python evals/run_eval.py --full             # the real run, 120 items
    uv run python evals/run_eval.py --scorer deepgram  # Deepgram only (free-er)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import socket
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery import rime_ws3  # noqa: E402
from evals import asr_score, channel  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evals/corpus/utterances.json"
RESULTS = ROOT / "evals/results"
CLIPS = RESULTS / "clips"
TRANSCRIPTS = RESULTS / "transcripts"

ARMS = {"baseline": "carrier_a", "samjha": "carrier_b"}

# Held constant across every request in both arms. Recorded verbatim into run_config.json —
# a run whose parameters are not on record is not evidence.
PARAMS = {
    "modelId": rime_ws3.MODEL_ID,      # pinned: defaults to mistv3, which has no Hindi
    "speaker": rime_ws3.SPEAKER,       # coda exposes exactly two Hindi voices
    "lang": rime_ws3.LANG,
    "samplingRate": rime_ws3.SAMPLE_RATE,
    "audioFormat": rime_ws3.AUDIO_FORMAT,
    "segment": "never",
    "timeScaleFactor": 1.0,            # speedAlpha is NEVER sent — three-way vendor trap
    "endpoint": rime_ws3.WS3,
}

# Measured on the day-1 battery clips: carrier sentences ran 3-9 s. Used only for the
# pre-flight cost estimate.
SECONDS_PER_CLIP = 6.0


def key_for(text: str) -> str:
    payload = json.dumps({"text": text, **PARAMS}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def mulaw_to_wav(pcm: bytes, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "mulaw", "-ar", "8000", "-ac", "1",
         "-i", "pipe:0", "-c:a", "pcm_mulaw", str(dest)],
        input=pcm, check=True, capture_output=True,
    )
    return dest


async def clip_for(text: str, arm: str, *, noise: Path | None, snr_db: float | None,
                   use_cache: bool) -> tuple[Path, bool]:
    """Synthesize (or reuse) one clip and push it through the channel. Returns (wav, cached)."""
    k = key_for(text)
    clean = CLIPS / arm / f"{k}.ulaw.wav"
    tel = CLIPS / arm / f"{k}.tel.wav"

    if use_cache and tel.exists() and clean.exists():
        return tel, True

    result = await rime_ws3.synthesize([text])
    if not result.audio:
        raise RuntimeError(f"Rime returned no audio for {text[:40]!r}: {result.frames[-3:]}")
    mulaw_to_wav(result.audio, clean)
    await asyncio.to_thread(channel.apply, clean, tel, noise=noise, snr_db=snr_db)
    return tel, False


def transcribe_cached(wav: Path, scorer: str, mode: str, use_cache: bool) -> tuple[str, bool]:
    """Disk-cached ASR. The cache is what makes a re-run free — Sarvam credit is the
    scarcest resource in this project, not time."""
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    cache = TRANSCRIPTS / f"{wav.stem}.{scorer}.{mode}.txt"
    if use_cache and cache.exists():
        return cache.read_text(encoding="utf-8"), True
    text = asr_score.transcribe(wav, scorer, mode)
    cache.write_text(text, encoding="utf-8")
    return text, False


def score_row(item: dict, wav: Path, scorer: str, use_cache: bool) -> dict:
    modes = asr_score.PASSES[scorer]
    texts, cached, matched, err = [], True, "", ""
    for mode in modes:
        try:
            t, c = transcribe_cached(wav, scorer, mode, use_cache)
        except Exception as e:  # a provider error is an error, never a scored miss
            t, c = "", False
            err = f"{type(e).__name__}: {e}"[:200]
        texts.append(t)
        cached &= c
        if not matched and t and asr_score.recovered(item, t):
            matched = mode
    return {
        "scorer": scorer,
        "pass_1": modes[0], "transcript_1": texts[0],
        "pass_2": modes[1] if len(modes) > 1 else "", "transcript_2": texts[1] if len(texts) > 1 else "",
        "matched_pass": matched, "recovered": bool(matched),
        "asr_cached": cached, "error": err,
    }


def stratified(items: list[dict], limit: int) -> list[dict]:
    """A smoke run must still touch all six categories, or it proves nothing."""
    by_kind = defaultdict(list)
    for it in items:
        by_kind[it["type"]].append(it)
    out, i = [], 0
    while len(out) < limit and any(by_kind.values()):
        for kind in sorted(by_kind):
            if i < len(by_kind[kind]) and len(out) < limit:
                out.append(by_kind[kind][i])
        i += 1
    return out


async def run(args) -> list[dict]:
    items = json.loads(CORPUS.read_text(encoding="utf-8"))
    if not args.full:
        items = stratified(items, args.limit)
    scorers = ["sarvam", "deepgram"] if args.scorer == "both" else [args.scorer]

    n_clips = len(items) * len(ARMS)
    sarvam_mins = n_clips * SECONDS_PER_CLIP * (2 if "sarvam" in scorers else 0) / 60
    print(f"{len(items)} utterances x {len(ARMS)} arms = {n_clips} clips"
          f"  (~{n_clips * SECONDS_PER_CLIP / 60:.1f} min of TTS audio)")
    print(f"Sarvam will receive ~{sarvam_mins:.1f} audio-minutes "
          f"({'2 passes per clip' if 'sarvam' in scorers else 'not used'}); "
          f"free credit is ~66 min. Cached clips and transcripts are not re-billed.")
    if not args.full:
        print("SMOKE RUN — pass --full for all 120 utterances.\n")
    else:
        print("FULL RUN.\n")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows: list[dict] = []
    sem = asyncio.Semaphore(args.concurrency)
    done = 0

    failures: list[str] = []

    async def one(item: dict, arm: str) -> list[dict]:
        nonlocal done
        text = item[ARMS[arm]]
        async with sem:
            try:
                wav, tts_cached = await clip_for(text, arm, noise=args.noise,
                                                 snr_db=args.snr, use_cache=not args.no_cache)
            except Exception as e:  # one bad socket must not discard 239 good clips
                try:
                    wav, tts_cached = await clip_for(text, arm, noise=args.noise,
                                                     snr_db=args.snr, use_cache=False)
                except Exception as e2:
                    done += 1
                    failures.append(f"{item['id']} {arm}: {type(e).__name__}: {e} / retry {e2}")
                    print(f"  [{done}/{len(items) * len(ARMS)}] {item['id']:<22} {arm:<9} SYNTH FAILED")
                    return []
            out = []
            for scorer in scorers:
                r = await asyncio.to_thread(score_row, item, wav, scorer, not args.no_cache)
                out.append({
                    "run_id": run_id, "item_id": item["id"], "type": item["type"],
                    "intended_reading": item["intended_reading"], "source": item["source"],
                    "doc_id": item["doc_id"], "arm": arm, "gt_value": item["value"],
                    "text_sent": text, "clip": str(wav.relative_to(ROOT)),
                    "tts_cached": tts_cached, "snr_db": "" if args.snr is None else args.snr,
                    **r,
                })
        done += 1
        print(f"  [{done}/{len(items) * len(ARMS)}] {item['id']:<22} {arm:<9}"
              f" {'cached' if tts_cached else 'fresh ':<7}"
              + " ".join(f"{r['scorer']}={'OK' if r['recovered'] else '--'}" for r in out))
        return out

    tasks = [one(it, arm) for it in items for arm in ARMS]
    for chunk in await asyncio.gather(*tasks):
        rows.extend(chunk)
    stale = RESULTS / "synth_failures.txt"
    if failures:
        print(f"\n{len(failures)} clip(s) could not be synthesized and are EXCLUDED, not "
              f"scored as misses:")
        for f in failures:
            print(f"  {f}")
        stale.write_text("\n".join(failures) + "\n", encoding="utf-8")
    else:
        stale.unlink(missing_ok=True)  # a clean run must not leave last run's failures behind
    return rows


def ver(rows: list[dict], **filters) -> tuple[float, int]:
    sel = [r for r in rows if all(r[k] == v for k, v in filters.items())]
    if not sel:
        return float("nan"), 0
    return 100.0 * sum(not r["recovered"] for r in sel) / len(sel), len(sel)


def write_outputs(rows: list[dict], args) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    run_id = rows[0]["run_id"]

    with (RESULTS / "item_level.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    host = rime_ws3.WS3.split("://")[1].split("/")[0]
    try:
        endpoint_ip = socket.gethostbyname(host)
    except OSError:
        endpoint_ip = "unresolved"

    config = {
        "run_id": run_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "rime_request": PARAMS,
        "endpoint_host": host,
        "endpoint_ip": endpoint_ip,
        "endpoint_region_note": "Rime has no India region; US West/East only. Trans-Pacific "
                                "RTT is reported separately by evals/latency.py.",
        "arms": {"baseline": "carrier_a (KFS as written)",
                 "samjha": "carrier_b (delivery-layer verbalisation)"},
        "chunking": "one flush segment per carrier sentence, IDENTICAL in both arms "
                    "(ACCEPTANCE.md §4 held-constant reading; see run_eval.py header)",
        "channel": {"sample_rate": channel.SAMPLE_RATE, "band_hz": list(channel.BAND),
                    "noise_before_codec": channel.NOISE_BEFORE_CODEC,
                    "noise_file": str(args.noise) if args.noise else None,
                    "snr_db": args.snr,
                    "snr_method": "whole-file RMS via ffmpeg volumedetect, NOT ITU-T P.56 "
                                  "active speech level (disclosed in ACCEPTANCE.md §5)"},
        "scorers": {"sarvam": {"model": "saaras:v4 (fallback saaras:v3)",
                               "language_code": "hi-IN", "modes": ["transcribe", "verbatim"]},
                    "deepgram": {"model": "nova-3", "language": "hi"}},
        "full_run": bool(args.full),
        "n_utterances": len({r["item_id"] for r in rows}),
        "cached_clips": sum(1 for r in rows if r["tts_cached"]),
        "fresh_clips": sum(1 for r in rows if not r["tts_cached"]),
        "versions": {p: version(p) for p in
                     ("websockets", "httpx", "indic-numtowords", "soundfile", "numpy",
                      "pydantic")},
        "python": sys.version.split()[0],
        "ffmpeg": subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
                            .stdout.splitlines()[0],
    }
    (RESULTS / "run_config.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    scorers = sorted({r["scorer"] for r in rows})
    kinds = sorted({r["type"] for r in rows})
    L = [f"# SAMJHA eval — {run_id}", ""]
    L.append(f"{config['n_utterances']} utterances x {len(ARMS)} arms x {len(scorers)} scorer(s)"
             f" = {len(rows)} scored rows."
             f"  {'FULL' if args.full else 'SMOKE'} run.")
    L.append("")
    L.append(f"Clips: {config['fresh_clips']} fresh, {config['cached_clips']} cached "
             f"(cached and uncached reported separately, as the checklist requires). "
             f"ASR: {sum(1 for r in rows if r['asr_cached'])} cached, "
             f"{sum(1 for r in rows if not r['asr_cached'])} fresh.")
    L += ["", "## Value Error Rate — overall", "",
          "Lower is better. VER = % of comprehension-critical values NOT recovered exactly.", ""]
    L.append("| scorer | " + " | ".join(ARMS) + " | delta |")
    L.append("|---|" + "---|" * (len(ARMS) + 1))
    for s in scorers:
        vals = [ver(rows, scorer=s, arm=a)[0] for a in ARMS]
        L.append(f"| {s} | " + " | ".join(f"{v:.1f}%" for v in vals)
                 + f" | {vals[0] - vals[1]:+.1f} pp |")

    L += ["", "## Value Error Rate — per category", "",
          "**This table is the deliverable.** The pre-registered claim was narrowed on day 1 "
          "to digit-sequence identifiers; the other four categories are reported as the "
          "negative result they are, not dropped.", ""]
    L.append("| category | intended reading | scorer | " + " | ".join(ARMS) + " | delta | n |")
    L.append("|---|---|---|" + "---|" * (len(ARMS) + 2))
    for k in kinds:
        reading = next(r["intended_reading"] for r in rows if r["type"] == k)
        for s in scorers:
            vals, ns = zip(*[ver(rows, scorer=s, arm=a, type=k) for a in ARMS])
            L.append(f"| {k} | {reading} | {s} | "
                     + " | ".join(f"{v:.1f}%" for v in vals)
                     + f" | {vals[0] - vals[1]:+.1f} pp | {ns[0]} |")

    if len(scorers) > 1:
        ag = asr_score.agreement(rows)
        L += ["", "## Inter-scorer agreement", "",
              "Two scorers that agree on almost everything are one measurement. Reported "
              "instead of quietly taking the better number.", "",
              f"- pairs compared: {ag['n']}",
              f"- observed agreement: {ag.get('observed_agreement', float('nan')):.1%}",
              f"- Cohen's kappa: {ag.get('cohens_kappa', float('nan')):.3f}",
              f"- recovery rate — sarvam {ag.get('sarvam_recovery', 0):.1%}, "
              f"deepgram {ag.get('deepgram_recovery', 0):.1%}"]

    ident = [r for r in rows if r["type"] == "account_identifier" and r["scorer"] == "sarvam"]
    if ident:
        L += ["", "## The inverse-text-normalization artifact", "",
              "A digit-sequence value that matches ONLY on Sarvam's `transcribe` pass may "
              "have been spoken as a *quantity* and reconstructed into digits by the "
              "provider's ITN. The borrower heard "
              "`नौ अरब पंद्रह करोड़ ...` and could not verify anything. By ASR that scores as "
              "recovered; by comprehension it is not. This is why the human panel is "
              "load-bearing (ACCEPTANCE.md §7, amendment 1).", ""]
        L.append("| arm | matched on transcribe only | matched on verbatim | not recovered |")
        L.append("|---|---|---|---|")
        for a in ARMS:
            sel = [r for r in ident if r["arm"] == a]
            L.append(f"| {a} | {sum(r['matched_pass'] == 'transcribe' for r in sel)} "
                     f"| {sum(r['matched_pass'] == 'verbatim' for r in sel)} "
                     f"| {sum(not r['recovered'] for r in sel)} |")

    errs = [r for r in rows if r["error"]]
    if errs:
        L += ["", f"## Provider errors ({len(errs)} rows)", "",
              "Errors are reported, never scored as misses.", ""]
        for r in errs[:10]:
            L.append(f"- `{r['item_id']}` {r['arm']} {r['scorer']}: {r['error']}")

    L += ["", "## Reproduce", "", "```", "make preflight", "make eval        # smoke",
          "uv run python evals/run_eval.py --full", "```", "",
          "Config, versions and endpoint: `results/run_config.json`. "
          "Per-row evidence: `results/item_level.csv`. "
          "Limitations: `evals/ACCEPTANCE.md` §8, written before the result was known.", ""]

    (RESULTS / "summary.md").write_text("\n".join(L), encoding="utf-8")

    # Every run is also archived under its own id. Without this, `make eval` (a smoke run
    # by default) silently overwrites the full run's evidence with six utterances — which
    # is exactly how a README ends up quoting a number no committed artifact supports.
    archive = RESULTS / "runs" / run_id
    archive.mkdir(parents=True, exist_ok=True)
    for name in ("item_level.csv", "summary.md", "run_config.json"):
        (archive / name).write_text((RESULTS / name).read_text(encoding="utf-8"),
                                    encoding="utf-8")

    print("\n" + "\n".join(L[:24]))
    print(f"\nwrote {RESULTS/'item_level.csv'}, {RESULTS/'summary.md'}, "
          f"{RESULTS/'run_config.json'}\narchived to {archive}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--full", action="store_true",
                   help="all 120 utterances. Required for the real run — the default is a smoke test.")
    p.add_argument("--limit", type=int, default=6,
                   help="smoke-run size, stratified across all six categories (default 6)")
    p.add_argument("--scorer", choices=["sarvam", "deepgram", "both"], default="both")
    p.add_argument("--concurrency", type=int, default=2,
                   help="parallel clips. Kept low: Rime free tier, and we are a guest there.")
    p.add_argument("--noise", type=Path, default=None,
                   help="noise WAV to mix in before the codec (ACCEPTANCE.md §5)")
    p.add_argument("--snr", type=float, default=None, help="target SNR in dB, e.g. 20/10/5")
    p.add_argument("--no-cache", action="store_true", help="re-synthesize and re-transcribe everything")
    args = p.parse_args()

    if (args.noise is None) != (args.snr is None):
        p.error("--noise and --snr must be given together")

    rows = asyncio.run(run(args))
    if not rows:
        print("no rows produced")
        return 1
    write_outputs(rows, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
