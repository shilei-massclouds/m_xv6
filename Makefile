MAKEFLAGS += --no-print-directory
PYTHON ?= python3
RUFF ?= ruff
PYRIGHT ?= pyright
DEBUG ?= n
export DEBUG

run:
	make clean_snap
	@$(PYTHON) main.py
	@$(PYTHON) main.py ap_init_origin
	@$(PYTHON) main.py initproc_origin --source bp
	@$(PYTHON) main.py initproc_origin --source ap

test:
	@$(RUFF) check .
	@$(RUFF) format --check .
	@$(PYRIGHT) .
	@$(PYTHON) -m pytest -q

clean:
	@find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
	@find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
	make clean_snap

clean_snap:
	@rm -f snapshots/*.snap

.PHONY: run test clean
