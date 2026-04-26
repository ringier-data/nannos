.env:
	@cp .env.template $@
.env.tmp: .env
	@sed 's/\$$/$$$$/g' .env > .env.tmp

include .env.tmp
export
export PYTHON=uv run python

.PHONY: app
app:
	uv run python -m voice_agent.server

.PHONY: install
install:
	uv sync --frozen --no-dev

.PHONY: dev
dev:
	uv sync

.PHONY: test-twilio
test-twilio:
	uv run python scripts/test_twilio_local.py --greet "Hello, please introduce yourself briefly." --output logs/twilio_response.wav
