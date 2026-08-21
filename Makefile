.PHONY: setup fmt lint type test docs release-check demo

setup:
	uv sync --all-groups --frozen

fmt:
	uv run ruff format --check .

lint:
	uv run ruff check .

type:
	uv run mypy

test:
	uv run pytest

docs:
	uv run python scripts/check_docs.py

release-check: docs
	uv lock --check
	uv run python scripts/check_release.py

demo:
	uv run python scripts/run_demo.py
