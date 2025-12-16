# Alloy Infrastructure Agents - Copilot Instructions

## Repository Overview

This is a monorepo containing multiple services and libraries for the Alloy Infrastructure Agents project:

- **playground-backend**: FastAPI backend service with PostgreSQL, DynamoDB, and authentication
- **playground-frontend**: React/TypeScript frontend with Vite
- **orchestrator-agent**: LangGraph-based agent orchestration service with document store
- **agent-creator**: A2A Server for creating and managing sub-agents
- **ringier-a2a-sdk**: Python SDK for Agent-to-Agent communication
- **infrastructure**: Ansible playbooks for deployment


## Deployment & Infrastructure as Code

All infrastructure is managed as code using Ansible playbooks and CloudFormation templates located in `/infrastructure`.

### Infrastructure Roles

Each component has a dedicated Ansible role in `infrastructure/roles/`:

- **basis**: Core infrastructure setup
  - PostgreSQL database provisioning and schema migrations
  - Network configuration and security groups
  - Base system packages and configuration
  
- **playground-backend**: Playground Backend service deployment
  - FastAPI application deployment
  - PostgreSQL connection configuration
  - DynamoDB tables for sessions and conversations
  - Application secrets from AWS SSM
  
- **orchestrator-agent**: Orchestrator Agent service deployment
  - LangGraph agent service deployment
  - PostgreSQL with pgvector for document store
  - DynamoDB checkpointers
  - S3 bucket configuration for checkpoint storage
  
- **agent-creator**: Agent Creator service deployment
  - A2A server deployment
  - DynamoDB and S3 for checkpoints
  - Integration with Playground Backend
  
- **playground-frontend**: Frontend application deployment
  - React/Vite static site deployment
  - Environment-specific configuration
  - Backend API endpoint configuration

### Deployment Process

- CloudFormation stacks define AWS resources (RDS, DynamoDB, S3, etc.)
- Ansible playbooks orchestrate provisioning and deployment
- Service-specific roles handle application deployment and configuration
- All secrets are managed via AWS SSM Parameter Store
- Migrations run automatically via Rambler during basis role execution

## Local Development Environment

**CRITICAL: The `/start-dev.sh` script is the single source of truth for local environment setup**

### When to Update start-dev.sh

Any changes that impact how services run locally MUST be reflected in `start-dev.sh`:

- **New environment variables**: Add to SSM fetching or default values
- **New secrets/credentials**: Add AWS SSM parameter fetching
- **Configuration changes**: Update .env generation logic
- **New service dependencies**: Add startup requirements
- **Port changes**: Update port checking logic
- **Database changes**: Update Docker container or RDS configuration
- **Changes to `.env` or `.env.template` files**: Ensure start-dev.sh populates correctly

### start-dev.sh Features

The script provides:
- Configurable local/remote service environments (--backend, --orchestrator, --agent-creator)
- Automatic Docker database management for local backend
- AWS SSM secret fetching for credentials
- Port conflict detection and resolution
- Process management with cleanup on interrupt
- Separate log files for each component
- Hot reload support for backend services

### Usage Examples

```bash
# Run everything locally
./start-dev.sh

# Run frontend locally, backend on dev
./start-dev.sh --backend dev

# Run frontend locally, all backends on staging
./start-dev.sh --backend stg --orchestrator stg --agent-creator stg

# Follow logs
tail -f logs/backend.log
tail -f logs/orchestrator.log
```


## Common Tasks

### Adding a New Environment Variable

1. Add to appropriate `.env.template` file
2. Update `start-dev.sh` to populate the variable:
   - For secrets: Add AWS SSM fetch
   - For config: Add default value or logic
3. Update Ansible Playbook and CloudFormation stacks if needed for deployed environments: `infrastructure/roles/`
4. Document in service-specific copilot instructions

### Adding a New Service

1. Create service directory under `app/`
2. Add `.github/copilot-instructions.md` for the service
3. Add `.env.template` with configuration variables
4. Update `start-dev.sh` to start the service
5. Add Ansible Playbook and CloudFormation stacks for deployment
6. Document dependencies and integration points

### Changing Database Schema

1. Create migration script in `infrastructure/roles/basis/files/ddl/scripts/`
2. Test migration on local database
3. Update SQLAlchemy models
4. Update repositories if needed
5. Run tests to verify changes

## Important Notes

- Never bypass repository pattern for database writes
- All secrets come from AWS SSM Parameter Store
- PostgreSQL credentials differ by role (service vs docstore)
- Frontend SDK is auto-generated from backend OpenAPI spec
- Orchestrator configuration depends on backend environment
