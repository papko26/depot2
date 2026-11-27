.PHONY: help install format lint typecheck test check

help:  ## Show this help (default)
	@awk '/^[[:alnum:]_-]+:.*## / { name = $$1; sub(/:$$/, "", name); sub(/^.*## /, ""); printf "  %-10s %s\n", name, $$0 }' $(MAKEFILE_LIST)

install:  ## Install locked development dependencies
	uv sync --locked

format:  ## Format Python files with Ruff
	uv run --locked ruff format .

lint:  ## Check Python files with Ruff
	uv run --locked ruff check .

typecheck:  ## Check package types with mypy
	uv run --locked mypy --python-version 3.8 -p depot2

test:  ## Run the test suite
	uv run --locked python -m unittest discover -s tests

check: lint typecheck test  ## Run lint, typecheck, and test
