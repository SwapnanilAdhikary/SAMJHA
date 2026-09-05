.PHONY: help setup preflight battery test eval demo-fixtures secrets

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
	@! git log -p --all | grep -inE '(rime|sarvam|deepgram)[_-]?api[_-]?key\s*[=:]\s*\S|sk-[a-zA-Z0-9]{16,}' \
	  || (echo "SECRET FOUND IN HISTORY — rotate it, do not just amend"; exit 1)
	@echo "no credentials in history"
