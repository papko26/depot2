.PHONY: help install format format-check lint typecheck test check

help:  ## Show this help (default)
	@awk '/^[[:alnum:]_-]+:.*## / { name = $$1; sub(/:$$/, "", name); sub(/^.*## /, ""); printf "  %-10s %s\n", name, $$0 }' $(MAKEFILE_LIST)

install:  ## Install locked development dependencies
	uv sync --locked

format:  ## Format Python files with Ruff
	uv run --locked ruff format .

format-check:  ## Check Python formatting with Ruff
	uv run --locked ruff format --check .

lint:  ## Apply safe Ruff fixes and report remaining issues
	uv run --locked ruff check --fix .

typecheck:  ## Check package types with mypy
	uv run --locked mypy --python-version 3.11 -p depot2

test:  ## Run the test suite in parallel and report line coverage
	uv run --locked pytest -q -n auto --cov=depot2 tests

check: lint format-check typecheck test  ## Check lint, formatting, types, and tests
