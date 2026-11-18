MAKEFLAGS += --no-print-directory
PYTHON ?= python3
RUFF ?= ruff
PYRIGHT ?= pyright
DEBUG ?= n
export DEBUG

run:
	@make clean_snap
	@$(PYTHON) main.py

test:
	@$(RUFF) check .
	@$(RUFF) format --check .
	@$(PYRIGHT) .
	@$(PYTHON) -m pytest -q

clean:
	@find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
	@find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
	@make clean_snap

clean_snap:
	@find snapshots -type f \( -name '*.snap' -o -name 'records.json' -o -name '.records.*.tmp' -o -name '.*.snap.*.tmp' \) -delete

.PHONY: run test clean clean_snap
