"""Entry point — allows `python -m main` and `uv run python -m main`."""

from argparse import ArgumentParser

from voice_agent.server import main

if __name__ == "__main__":
    parser = ArgumentParser(description="Run the voice agent server.")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development.")
    args = parser.parse_args()
    main(reload=args.reload)
