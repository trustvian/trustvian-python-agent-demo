# The whole developer experience is `make demo`. Everything else exists to
# support it or to check it.

# No SHELL pin. Every recipe just invokes a script, and each of those picks
# its own interpreter through `#!/usr/bin/env bash` — which finds a modern
# bash when one is installed and falls back to the system's otherwise. The
# scripts themselves are written to run under bash 3.2, which is what
# /bin/bash still is on macOS.
.DEFAULT_GOAL := help

.PHONY: help demo smoke scenario bootstrap clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

demo: ## Run the full reference demo and leave the runtime up for inspection
	@./scripts/demo.sh

smoke: ## Run the same path non-interactively and assert every guarantee
	@./scripts/smoke.sh

scenario: ## Run one behavioral scenario: make scenario SCENARIO=scenarios/<name>.yaml
	@./scripts/bootstrap.sh >/dev/null
	@.demo/tools-venv/bin/python tools/scenario.py \
		"$(or $(SCENARIO),scenarios/support-fixture.yaml)" \
		--results .demo/scenario-results.json

bootstrap: ## Build Trustvian binaries and create the demo Python environment
	@./scripts/bootstrap.sh

clean: ## Remove every generated artifact, including the evaluation database
	@rm -rf .demo .runtime .trustvian
	@echo "Removed .demo/, .runtime/ and .trustvian/"
