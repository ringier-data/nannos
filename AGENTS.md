# Alloy Infrastructure Agents

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Common Conventions

- **Python Commands**: Always use `uv` for all Python operations (`uv sync`, `uv run pytest`, `uv run python`)
- **File Writing**: NEVER use heredoc (`cat << EOF`) to write files — causes fatal errors. Use incremental edits instead.

## Repository Overview

This is a monorepo containing multiple services and libraries for the Nannos project. See `/packages` for each individual package.

## Skills

- **add-package** (`.github/skills/add-package/SKILL.md`): Checklist and procedure for adding a new package to the monorepo — covers directory setup, release-helpers.sh, justfile, and optional k8s manifests.
