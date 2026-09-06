.PHONY: help setup preflight battery test eval demo-fixtures secrets talk

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
