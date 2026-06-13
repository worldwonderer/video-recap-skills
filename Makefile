.PHONY: recap demo package lint test doctor clean help
RECAP := skills/video-recap/scripts/recap.py
SKILLS := video-understanding video-script video-cut video-voiceover video-assemble video-recap
TEST_GROUPS := understanding cut voiceover assemble script orchestrator
VIDEO ?=

recap: ## make recap VIDEO=<path>
	@test -n "$(VIDEO)" || (echo "Usage: make recap VIDEO=<path>"; exit 1)
	python3 $(RECAP) $(abspath $(VIDEO))

demo: ## run demo
	$(MAKE) recap VIDEO=demo/demo.mp4

package: ## package every skill in the bundle
	@for s in $(SKILLS); do \
		~/.claude/skills/skill-creator/scripts/package_skill.py skills/$$s; \
	done

lint: ## lint all skill scripts + tests
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check skills/*/scripts tests; \
	else \
		python3 -m pyflakes skills/*/scripts/*.py; \
	fi

test: ## run tests (one isolated process per skill — each skill ships its own lib)
	@set -e; for g in $(TEST_GROUPS); do \
		echo "== $$g =="; python3 -m pytest tests/$$g -q; \
	done

doctor: ## check runtime prerequisites
	python3 $(RECAP) --doctor

clean: ## clean
	rm -rf skills/*/scripts/work_dir_* recap_*.mp4 *.skill
	find skills tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

help: ## show help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
