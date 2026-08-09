.PHONY: setup fmt lint type test demo

setup:
	uv sync --all-groups --frozen

fmt:
	uv run ruff format .

lint:
	uv run ruff check .

type:
	uv run mypy

test:
	uv run pytest

demo:
	@printf '%s\n' 'WP-01 scaffold only: the runnable episode demo is introduced in WP-10.'
