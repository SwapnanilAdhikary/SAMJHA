.PHONY: help setup preflight battery test eval demo-fixtures secrets talk \
        serve agent vendor fixtures prompts e2e video zip

# Pinned, with its hash recorded, because there is no build step that could resolve a
# version at deploy time. Bump both together.
LIVEKIT_CLIENT_VERSION := 2.15.7
LIVEKIT_CLIENT_SHA256  := 09dce29e6e551820f92dd63003669b5a8ef6e6014af06d6b5244634c8872f2a3

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

setup: ## Install the pinned toolchain
	uv sync

preflight: ## Verify (modelId, speaker, lang) against Rime's LIVE catalog. Must pass before any run.
	uv run python -m delivery.config_guard

battery: preflight ## Day-1 test battery. Generates clips you then LISTEN to.
	uv run python scripts/battery.py

test: ## Normaliser regression suite
	uv run pytest -q

channel: ## Self-check the telephone-channel chain
	uv run python -m evals.channel

eval: preflight ## Full A/B, one command. Reproduces every number in the README.
	uv run python evals/run_eval.py

ZIP ?= ../samjha-submission.zip

zip: secrets ## Package the repo for submission. Runs `make secrets` first, refuses to ship .env.
	@# Excluded because it is regenerable, runtime state, or private:
	@#   .venv (685M — `uv sync`)          evals/results/clips (34M — `make eval`)
	@#   data/ (uploaded documents + db)   events/ (per-call logs)
	@#   video/_raw, demo/audio            caches, __pycache__, .DS_Store
	@# .git IS included: the commit timestamp on evals/ACCEPTANCE.md is what shows the
	@# test was written before the result, and that is a scoring artifact.
	@rm -f $(ZIP)
	@zip -r -q $(ZIP) . \
	  -x '.venv/*' -x '*/__pycache__/*' -x '__pycache__/*' -x '*.pyc' \
	  -x '.pytest_cache/*' -x '.ruff_cache/*' -x '*.DS_Store' \
	  -x '.env' -x 'data/*' -x 'events/*' -x 'evals/results/clips/*' \
	  -x 'video/_raw/*' -x 'demo/audio/*' -x 'demo/full_call.wav'
	@unzip -l $(ZIP) | grep -qE ' \.env$$' \
	  && (echo "REFUSING TO SHIP: .env is in the archive"; rm -f $(ZIP); exit 1) || true
	@echo "$(ZIP)  $$(du -h $(ZIP) | cut -f1)  ($$(unzip -l $(ZIP) | tail -1 | awk '{print $$2}') files)"

secrets: ## Fail if a credential ever touched git history
	@# Requires 16+ key-like chars after the '=', so documentation and placeholders
	@# (RIME_API_KEY=..., =<your-key>, =) do not trip it. A scan that cries wolf is a
	@# scan people stop reading.
	@! git log -p --all \
	  | grep -inE "(rime|sarvam|deepgram|livekit|openrouter)[_-]?api[_-]?(key|secret)[\"']?[[:space:]]*[=:][[:space:]]*[\"']?[A-Za-z0-9_-]{16,}|sk-[a-zA-Z0-9]{16,}" \
	  || (echo "SECRET FOUND IN HISTORY — rotate the key, do not just amend the commit"; exit 1)
	@echo "no credentials in history"

demo-fixtures: ## Scripted stress cases, no network: barge-in leaves PARTIALLY_HEARD, rushed consent refused
	uv run python -m agent.consent_fsm
	uv run python -m agent.rushed_consent

talk: ## BE THE BORROWER — a real call: Rime speaks, you answer, consent is decided
	@set -a; . ./.env; set +a; uv run python scripts/talk.py $(ARGS)

video: ## Record a demo video of the whole flow (needs `make serve` + playwright)
	@# Drives the real UI in Chromium while a real Rime-backed run paces the events.
	@# Browser capture has no audio, so the same run's Rime track is muxed in.
	@set -a; . ./.env; set +a; uv run python scripts/record_demo.py $(ARGS)

e2e: ## Drive the whole flow against any KFS. Needs `make serve`. ARGS="--doc x.pdf --drive"
	@# Defaults to a random fixture in either format. --drive SPEAKS THROUGH RIME and
	@# asserts the sealed record; add --play to hear it, --fake-audio to work offline.
	@# Sources .env, because --drive needs RIME_API_KEY.
	@set -a; . ./.env; set +a; uv run python scripts/e2e.py $(ARGS)

serve: ## The API and all three web surfaces: / (judge), /intake (helper), /c/{id} (borrower)
	uv run uvicorn api.main:app --reload --port 8000

agent: ## The consent agent worker. REQUIRED for a real call — dispatch is explicit.
	@# Without a registered worker, POST /calls/{id}/dispatch succeeds and the borrower
	@# waits on a screen that never changes. `make serve` alone is not enough.
	uv run python -m agent.main dev

vendor: ## Fetch the pinned livekit-client into web/vendor/ and verify its hash
	@mkdir -p web/vendor
	@curl -fsSL "https://cdn.jsdelivr.net/npm/livekit-client@$(LIVEKIT_CLIENT_VERSION)/dist/livekit-client.umd.min.js" \
	  -o web/vendor/livekit-client.umd.min.js
	@echo "$(LIVEKIT_CLIENT_SHA256)  web/vendor/livekit-client.umd.min.js" | shasum -a 256 -c - \
	  || (echo "HASH MISMATCH — refusing to ship an unverified browser bundle"; exit 1)

fixtures: ## Regenerate the synthetic KFS documents in both formats
	PYTHONPATH=. uv run python fixtures/synthetic/_to_pdf.py
	PYTHONPATH=. uv run python fixtures/synthetic/_to_docx.py

prompts: ## Synthesize the borrower page's spoken Hindi prompts through Rime (needs RIME_API_KEY)
	@set -a; . ./.env; set +a; uv run python -m web.prompts
