"""Preflight: verify our (modelId, speaker, lang) triple exists in Rime's LIVE catalog.

Why this exists. Rime's docs are explicit that a bad pairing does not reliably error:

    "Do not rely on this error to catch a bad pairing. It is not returned for every model
     or transport, so an unsupported combination of speaker, modelId and lang can be
     accepted and synthesized."

And `modelId` defaults to `mistv3` — which has no Hindi at all — so a single typo silently
routes the whole experiment to the wrong model while still returning working audio. That
would destroy the "model held constant" premise of the A/B without any visible failure.

Hardcoding a speaker list is what this guards against, so the catalog is fetched live.
Both endpoints are public and need no auth.

    uv run python -m delivery.config_guard        # exits non-zero on mismatch
"""

from __future__ import annotations

import sys

import httpx

ALL_V2 = "https://users.rime.ai/data/voices/all-v2.json"
VOICE_DETAILS = "https://users.rime.ai/data/voices/voice_details.json"

# What the agent and the eval both send. Single source of truth.
MODEL_ID = "coda"
SPEAKER = "taru"
LANG = "hi"  # BCP 47, what we send on the wire

# Rime's catalog keys languages by 3-letter ISO 639-2 while the API expects BCP 47.
# Both are accepted on the wire; only the catalog needs the mapping.
LANG_TO_CATALOG = {"hi": "hin", "en": "eng", "es": "spa", "fr": "fra", "de": "ger"}

# Retired 2026-08-15. Requests using these are silently served by Coda, which has a
# completely different speaker catalog — so a stale speaker name yields wrong-voice audio
# rather than an error.
RETIRED_MODELS = {"arcana", "arcanav2", "arcanav3"}


def fetch(url: str) -> object:
    r = httpx.get(url, timeout=20.0)
    r.raise_for_status()
    return r.json()


def real_speakers(catalog: dict, model: str, catalog_lang: str) -> list[str]:
    """Speakers for (model, lang), with the catalog's phantom entries removed.

    all-v2.json has a data bug: the language code is injected into its own speaker list,
    so coda.hin reads ["hin", "nadi", "taru"]. Iterating it naively produces a speaker
    literally named "hin", which is not a voice. Confirmed by arithmetic: dropping the
    three phantoms (coda.ara/hin/ita) takes Coda from 256 to the documented 253.
    """
    names = catalog.get(model, {}).get(catalog_lang, [])
    return [s for s in names if s != catalog_lang]


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []

    try:
        catalog = fetch(ALL_V2)
        details = fetch(VOICE_DETAILS)
    except Exception as e:  # network, DNS, 5xx — all fatal for a preflight
        print(f"PREFLIGHT FAIL: could not fetch Rime catalog: {type(e).__name__}: {e}")
        return 2

    catalog_lang = LANG_TO_CATALOG.get(LANG, LANG)

    if MODEL_ID in RETIRED_MODELS:
        problems.append(f"modelId {MODEL_ID!r} is RETIRED and is silently served by Coda.")

    if MODEL_ID not in catalog:
        problems.append(f"modelId {MODEL_ID!r} not in live catalog. Present: {sorted(catalog)}")
    else:
        speakers = real_speakers(catalog, MODEL_ID, catalog_lang)
        if not speakers:
            problems.append(
                f"{MODEL_ID!r} serves no voices for lang {catalog_lang!r}. "
                f"Languages available: {sorted(catalog[MODEL_ID])}"
            )
        elif SPEAKER not in speakers:
            problems.append(
                f"speaker {SPEAKER!r} not served by {MODEL_ID!r}/{catalog_lang!r}. "
                f"Available: {speakers}"
            )
        else:
            notes.append(f"{MODEL_ID}/{catalog_lang} voices: {speakers}")

    # voice_details.json is the second, independent source. If the speaker is missing here
    # it may be a stale entry in all-v2.json — worth knowing before a demo.
    matching = [
        d for d in details
        if d.get("speaker") == SPEAKER and d.get("modelId") == MODEL_ID
        and d.get("lang") == catalog_lang
    ]
    if not matching:
        problems.append(
            f"{SPEAKER!r} has no voice_details record for {MODEL_ID}/{catalog_lang}. "
            "It may exist only as a stale entry in all-v2.json."
        )
    else:
        d = matching[0]
        notes.append(
            f"{SPEAKER}: {d.get('gender')}, {d.get('age')}, {d.get('country')} "
            f"— {d.get('description')}"
        )

    banner = f"{MODEL_ID} / {SPEAKER} / {LANG}"
    for n in notes:
        print(f"  {n}")

    if problems:
        print(f"\nPREFLIGHT FAIL: {banner}")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"\nPREFLIGHT OK: {banner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
