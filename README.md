# rcplus-nannos-infrastructure-agents

Current version: **v0.7.0**

## Description

Infrastructure for Nannos Agents Framework - a comprehensive monorepo containing multiple services and libraries for agent orchestration, including the playground backend, orchestrator agent, agent creator, and specialized agents like the Alloy Agent.

## Quick Start

### Local Development

Start all services locally:

```bash
./start-dev.sh
```

This will start:
- **Frontend**: http://localhost:5173 (React/Vite)
- **Backend**: http://localhost:5001 (FastAPI)
- **Orchestrator**: http://localhost:10001 (LangGraph)
- **Agent Creator**: http://localhost:8080 (A2A Server)
- **Alloy Agent**: http://localhost:5004 (A2A Server)

### Environment Options

Run services in different environments:

```bash
# Run frontend locally, backend on dev
./start-dev.sh --backend dev

# Run frontend locally, all backends on staging
./start-dev.sh --backend stg --orchestrator stg --agent-creator stg --alloy stg

# Mix and match environments
./start-dev.sh --backend dev --orchestrator local --agent-creator stg
```

Available environments: `local`, `dev`, `stg`, `prod`

### Debug Mode

Start services with debugging enabled:

```bash
./start-dev.sh --debug
```

This starts all local services with debugpy listening on:
- Backend: port 5678
- Orchestrator: port 5679
- Agent Creator: port 5680
- Alloy Agent: port 5681

**Attach VS Code Debugger:**
1. Press ⇧⌘D (macOS) or Ctrl+Shift+D (Windows/Linux)
2. Select "Debug: All Services" from the dropdown
3. Press F5 to attach to all services

**Set Breakpoints:**
- Open any Python file in the service you want to debug
- Click in the gutter (left of line numbers) to set breakpoints
- Code execution will pause when breakpoints are hit

See [`.vscode/DEBUG.md`](.vscode/DEBUG.md) for complete debugging documentation.

### Following Logs

Each component logs to its own file:

```bash
tail -f logs/frontend.log
tail -f logs/backend.log
tail -f logs/orchestrator.log
tail -f logs/agent-creator.log
tail -f logs/alloy.log
```

### Stopping Services

Press `Ctrl+C` to stop all components gracefully.

## Prerequisites

- **AWS Profile**: Export your AWS profile before running:
  ```bash
  export AWS_PROFILE=your-profile-name
  ```

- **Docker**: Required for local backend (PostgreSQL database)
  ```bash
  # The script will automatically start the database container
  # Or manually: docker run --rm -d --name playground-db -p 5432:5432 nannos-infrastructure-agents-test-database:latest
  ```

- **Python**: Use `uv` for all Python operations
  ```bash
  cd app/playground-backend
  uv sync  # Install dependencies
  ```

- **Node.js**: For frontend development
  ```bash
  cd app/playground-frontend
  npm install
  ```
