.PHONY: install format lint typecheck test check

install:
	uv sync --locked

format:
	uv run --locked ruff format .

lint:
	uv run --locked ruff check .

typecheck:
	uv run --locked mypy --python-version 3.8 -p depot2

test:
	uv run --locked python -m unittest discover -s tests

check: lint typecheck test
