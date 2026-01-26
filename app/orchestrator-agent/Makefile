.PHONY: nltk-data
nltk-data:  ## Download NLTK punkt tokenizer
	uv run python -m nltk.downloader punkt

.PHONY: lint
lint: nltk-data
	uv run ruff check .
	uv run mypy . --explicit-package-bases

.PHONY: python
python:
	uv run python

.PHONY: test
test:
	uv run pytest tests/ -v

.PHONY: test-cov
test-cov:
	uv run pytest tests/ --cov=app --cov-report=term-missing --cov-report=html
