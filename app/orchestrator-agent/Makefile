.env:  ## Generate .env file
	@cp .env.template $@
	aws ssm get-parameter --name /alloy/data-sharing/openai-api-key-chatgpt-4o --output json --with-decryption | jq -r .Parameter.Value -c | awk '{print "AZURE_OPENAI_API_KEY="$$0}' >> .env
	aws ssm get-parameter --name /alloy/infrastructure-agents/langsmith-api-key --output json --with-decryption | jq -r .Parameter.Value -c | awk '{print "LANGSMITH_API_KEY="$$0}' >> .env
	aws ssm get-parameter --name /alloy/infrastructure-agents/oidc-orchestrator-client-secret --output json --with-decryption | jq -r .Parameter.Value -c | awk '{print "OIDC_CLIENT_SECRET="$$0}' >> .env
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
	uv run python

.PHONY: start-app
start-app:
	uv run python main.py --host localhost

.PHONY: stop-app
stop-app:
	# stop app by killing python process listening on port 10001
	lsof -ti:10001 | xargs -r kill -9 || true

.PHONY: restart-app
restart-app: stop-app app

.PHONY: start-app-detached
start-app-detached:
	# run app and log output to app.log in detached mode
	uv run python main.py &> app.log &

.PHONY: restart-app-detached
restart-app-detached: stop-app start-app-detached

.PHONY: test
test:
	uv run pytest tests/ -v

.PHONY: test-cov
test-cov:
	uv run pytest tests/ --cov=app --cov-report=term-missing --cov-report=html

.PHONY: test-watch
test-watch:
	uv run pytest-watch tests/ -v

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
