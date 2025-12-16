.PHONY: help install dev test lint

help:
	@echo "Available commands:"
	@echo "  make install      - Install dependencies"
	@echo "  make install-dev          - Install development dependencies"
	@echo "  make test         - Run tests"
	@echo "  make lint         - Run linters"

install:
	uv sync --frozen

dev: install
	uv sync --dev

test:
	uv run pytest tests/ -v

lint:
	uvx run ruff check .
	uvx ruff check .
	uv run mypy . --explicit-package-bases
	# uvx ty check
