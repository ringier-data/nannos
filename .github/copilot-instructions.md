# Repository Instructions

Nannos is a multi-agent AI orchestration platform monorepo. Packages live under `packages/`.

## Build & Test

### Python packages (uv)

```bash
cd packages/<package-name>
uv sync                    # Install dependencies
uv run pytest tests/ -v    # Run tests
```

All Python packages use `uv` — never `pip` or `poetry`.

### Node packages (npm)

```bash
cd packages/<package-name>
npm ci                     # Install dependencies
npm run build              # Build
npm test                   # Test (Jest)
npm run lint               # Lint (ESLint)
```

### Frontend API clients

Frontend packages auto-generate TypeScript clients from OpenAPI specs:
```bash
npm run gen-sdk            # Regenerate after backend API changes
```

## Database Migrations

All services use **Rambler** for SQL migrations in `sqlmigrations/` directories.
- Format: `###_description.sql` (e.g., `053_create_skill_activations.sql`)
- Always include `-- rambler up` and `-- rambler down` sections
- Migrations run automatically on container startup via `entrypoint.sh`
- PostgreSQL enum values use `ALTER TYPE ... ADD VALUE` (cannot be removed)

## Versioning & Release

- Each package versioned independently in `pyproject.toml` or `package.json`
- Docker images: `ghcr.io/ringier-data/nannos-<package-name>`
- Git tags: `<package-name>/v<semver>` (e.g., `orchestrator-agent/v0.10.0`)
- `just changed` — list packages needing release
- `just release` — bump, tag, build all changed packages

## Code Review Guidelines

- All database writes MUST use the repository pattern (console-backend) for audit logging
- Never bypass `HumanInTheLoopMiddleware` guards on skill/playbook tools
- `checkpoint_ns` must be `""` for standalone LangGraph agents (not subgraphs)
- Never use heredoc (`cat << EOF`) to write files — causes fatal errors
- Type hints required on all Python function signatures
- Use `async/await` for all I/O operations

## Package Dependencies

```
ringier-a2a-sdk → agent-common → { orchestrator-agent, agent-creator, agent-runner }
```

Changes to `ringier-a2a-sdk` or `agent-common` affect all downstream services.
