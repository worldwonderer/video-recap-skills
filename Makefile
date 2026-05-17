.PHONY: recap demo package lint test clean help
SKILL_DIR := skills/video-recap
VIDEO ?=

recap: ## make recap VIDEO=<path>
	@test -n "$(VIDEO)" || (echo "Usage: make recap VIDEO=<path>"; exit 1)
	cd $(SKILL_DIR) && python3 scripts/video_recap.py $(abspath $(VIDEO)) \
		--agent-mode --tts edge-tts

demo: ## run demo
	$(MAKE) recap VIDEO=demo/demo.mp4

package: ## package skill
	~/.claude/skills/skill-creator/scripts/package_skill.py $(SKILL_DIR)

lint: ## lint
	cd $(SKILL_DIR) && if command -v ruff >/dev/null 2>&1; then \
		ruff check scripts tests; \
	else \
		python3 -m pyflakes scripts/*.py tests/*.py; \
	fi

test: ## run tests
	cd $(SKILL_DIR) && python3 -m pytest tests/ -v

clean: ## clean
	rm -rf $(SKILL_DIR)/work_dir_* recap_*.mp4 *.skill

help: ## show help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
