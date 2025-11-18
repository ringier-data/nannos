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
	uv run python -m a2a_server.main --port 9996

# .PHONY: test
# test:
# 	uv tool install tox --with tox-uv
# 	tox run -e lambda
# 	tox run -e api

# .PHONY: test-api
# test-api:
# 	uv tool install tox --with tox-uv
# 	tox run -e api

# .PHONY: test-lambda
# test-lambda:
# 	uv tool install tox --with tox-uv
# 	tox run -e lambda

# .PHONY: install
# install:
# 	uv sync --frozen --no-dev

# .PHONY: profile-lambda
# profile-lambda:
# 	uv run pyinstrument -r html scripts/profile_lambda.py

# .PHONY: install-dev
# install-dev:
# 	uv sync --frozen

# .PHONY: api
# api:
# 	uv run uvicorn app.main:app --reload --port 8001

# .PHONY: api-otel
# api-otel:
# 	uv run opentelemetry-instrument python app/main.py --port 8001

# .PHONY: debug-api
# debug-api:
# 	uv run python -m debugpy --listen 5678 --wait-for-client -m uvicorn app.main:app --reload --port 8001

# .PHONY: debug-lambda
# debug-lambda:
# 	NAONOUS_API_BASE_URI=http://localhost:8001 uv run python -m debugpy --listen 5679 --wait-for-client scripts/debug.py &
# 	$(MAKE) debug-api

# .PHONY: slides
# slides:
# 	uv run python scripts/slides.py
