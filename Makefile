# The whole developer experience is `make demo`. Everything else exists to
# support it or to check it.

# No SHELL pin. Every recipe just invokes a script, and each of those picks
# its own interpreter through `#!/usr/bin/env bash` — which finds a modern
# bash when one is installed and falls back to the system's otherwise. The
# scripts themselves are written to run under bash 3.2, which is what
# /bin/bash still is on macOS.
.DEFAULT_GOAL := help

.PHONY: help demo smoke scenario stability release-smoke bootstrap clean

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
		--results .demo/scenario-results.json; \
	status=$$?; \
	if [ $$status -eq 1 ]; then \
		echo "make: the recipe below exited 1 because the gate said FAIL."; \
	fi; \
	exit $$status

stability: ## Measure how often an unchanged agent fails a gate against itself: make stability RUNS=10
	@./scripts/bootstrap.sh >/dev/null
	@.demo/tools-venv/bin/python tools/stability.py \
		"$(or $(SCENARIO),scenarios/stability.yaml)" \
		--runs "$(or $(RUNS),10)" \
		--temperature "$(or $(TEMPERATURE),0.7)" \
		--results .demo/stability-results.json

release-smoke: ## Prove the demo runs from a downloaded release: make release-smoke TRUSTVIAN_RELEASE_DIR=<extracted archive>
	@# Run by hand, deliberately not in CI. CI builds from the sibling checkout
	@# so it catches a Trustvian change that breaks this demo; a release-pinned
	@# job would be blind to exactly that. The cost is that this path is
	@# documented and hand-tested rather than guarded, which is the trade.
	@test -n "$(TRUSTVIAN_RELEASE_DIR)" || { \
		printf 'error: set TRUSTVIAN_RELEASE_DIR to an extracted release archive\n\n'; \
		printf '    tar xzf trustvian_v0.10.0_darwin_arm64.tar.gz\n'; \
		printf '    make release-smoke TRUSTVIAN_RELEASE_DIR=$$PWD/trustvian_v0.10.0_darwin_arm64\n\n'; \
		exit 2; }
	@# A clean .demo/bin, so this cannot pass on binaries a checkout build left
	@# behind — which is the one way this target could lie.
	rm -rf .demo/bin
	env -u TRUSTVIAN_DIR TRUSTVIAN_RELEASE_DIR="$(TRUSTVIAN_RELEASE_DIR)" ./scripts/bootstrap.sh
	env -u TRUSTVIAN_DIR TRUSTVIAN_RELEASE_DIR="$(TRUSTVIAN_RELEASE_DIR)" ./scripts/smoke.sh
	@printf '\nrelease-smoke: the demo ran from %s\n' "$(TRUSTVIAN_RELEASE_DIR)"

bootstrap: ## Build Trustvian binaries and create the demo Python environment
	@./scripts/bootstrap.sh

clean: ## Remove every generated artifact, including the evaluation database
	@./scripts/clean.sh
