.PHONY: help install dev test lint run

help:
	@echo "Available commands:"
	@echo "  make install      - Install dependencies"
	@echo "  make dev          - Install development dependencies"
	@echo "  make test         - Run tests"
	@echo "  make lint         - Run linters"
	@echo "  make run          - Run the service locally (port 5005)"

install:
	uv sync --frozen

dev: install
	uv sync --dev

test:
	uv run pytest tests/ -v

lint:
	uv run ruff check .
	uv run mypy . --explicit-package-bases

run:
	uv run python main.py
