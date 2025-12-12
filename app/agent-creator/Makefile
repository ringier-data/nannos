.env:  ## Generate .env file
	@cp .env.template $@
.env.tmp: .env
	# Generate .env.tmp file from .env: replace $ with $$
	@sed 's/\$$/$$$$/g' .env > .env.tmp

include .env.tmp
export
export PYTHON=uv run python

.PHONY: lint
lint:
	uv run ruff check .
	uv run mypy . --explicit-package-bases

.PHONY: python
python:
	python

.PHONY: app
app:
	uv run python -m main --port 8080

.PHONY: test
test:
	uv run pytest tests/

.PHONY: install
install:
	uv sync --frozen --no-dev

.PHONY: install-dev
install-dev:
	uv sync --frozen
