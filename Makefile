.PHONY: help install dev agents probe test lint fmt fmt-check fakes gate ci frontend rehearse

# Prefer the project venv when it exists, so every target works from a clean checkout after
# `make install` whether or not the shell has activated it. Bare `python` also breaks on hosts
# where only `python3` is on PATH, which is most of them now.
VENV := .venv
PY   := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
RUFF := $(if $(wildcard $(VENV)/bin/ruff),$(VENV)/bin/ruff,ruff)

# Default target: `make` on its own should teach, not guess.
help:
	@echo "Overwatch — make targets"
	@echo ""
	@echo "  install     create .venv and install dependencies + dev tools"
	@echo "  dev         run the API with reload on :8000"
	@echo "  frontend    build the React landing page"
	@echo "  agents      launch all 5 Band agents in tmux panes"
	@echo ""
	@echo "  test        pytest"
	@echo "  lint        ruff check"
	@echo "  fmt         ruff format (writes)"
	@echo "  fakes       list outstanding '# FAKE:' stubs; must print 'clean'"
	@echo "  gate        fakes + lint + test — run before every milestone"
	@echo "  ci          everything CI runs, locally"
	@echo ""
	@echo "  probe SVC=terac|band|replay         one real API call, raw output"
	@echo "  rehearse    full pipeline against simulated raters (needs a server on :8013)"

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	.venv/bin/pip install "ruff==0.8.*"
	@echo "Done. Activate with: source .venv/bin/activate"

dev:
	$(PY) -m uvicorn app.main:app --reload --port 8000

# The React landing page. `/` falls back to app/templates/landing.html without this, so a
# skipped build degrades the design rather than breaking the site.
frontend:
	cd front-end && npm install && npm run build

agents:
	@echo "5 agents, 5 processes. One WebSocket per agent_id — never share."
	tmux new-session -d -s ow '$(PY) -m app.agents.scout'
	tmux split-window -t ow '$(PY) -m app.agents.triage'
	tmux split-window -t ow '$(PY) -m app.agents.recruiter'
	tmux split-window -t ow '$(PY) -m app.agents.bursar'
	tmux split-window -t ow '$(PY) -m app.agents.critic'
	tmux select-layout -t ow tiled
	tmux attach -t ow

# make probe SVC=terac | band | replay
# PYTHONPATH=. because running a file inside scripts/ puts scripts/ on sys.path, not the repo
# root, so `from app.config import settings` raises ModuleNotFoundError. Set here rather than
# with a sys.path hack in each probe.
# ARGS passes probe flags through: make probe SVC=replay ARGS="--create https://example.com"
probe:
	PYTHONPATH=. $(PY) scripts/probe_$(SVC).py $(ARGS)

test:
	$(PY) -m pytest

lint:
	$(RUFF) check .

fmt:
	$(RUFF) format .

fmt-check:
	$(RUFF) format --check .

# Full pipeline against simulated raters. Proves the wiring, produces no result.
# Needs a server on :8013 with BUG_SOURCE=seed.
rehearse:
	PYTHONPATH=. $(PY) scripts/rehearse_experiment.py --base http://localhost:8013

# Must print "clean" before any milestone is called done.
fakes:
	@grep -rn --include='*.py' --include='*.ts' --include='*.tsx' --include='*.html' \
		"# FAKE:" app/ scripts/ workflows/ front-end/src/ || echo "clean"

# Run before every gate. Prints time + outstanding stubs.
gate: fakes lint test
	@date "+%H:%M — gate check"

# What CI runs, so a red build can be reproduced without pushing.
ci: lint fmt-check test
	@echo "CI-equivalent checks passed."
