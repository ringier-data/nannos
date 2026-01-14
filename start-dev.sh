#!/usr/bin/env zsh

# Local Development Environment Launcher
# Starts components with configurable local/remote backend services

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
BACKEND_ENV="local"
ORCHESTRATOR_ENV="local"
AGENT_CREATOR_ENV="local"
ALLOY_ENV="local"
LOG_DIR="./logs"
HELP=false
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Environment URLs
typeset -A BACKEND_URLS
BACKEND_URLS=(
    local "http://localhost:5001"
    dev "https://playground-backend.d.nannos.rcplus.io"
    stg "https://playground-backend.s.nannos.rcplus.io"
    prod "https://playground-backend.nannos.rcplus.io"
)

typeset -A ORCHESTRATOR_DOMAINS
ORCHESTRATOR_DOMAINS=(
    local "localhost:10001"
    dev "orchestrator.d.nannos.rcplus.io"
    stg "orchestrator.s.nannos.rcplus.io"
    prod "orchestrator.nannos.rcplus.io"
)

typeset -A AGENT_CREATOR_URLS
AGENT_CREATOR_URLS=(
    local "http://localhost:8080"
    dev "https://agent-creator.d.nannos.rcplus.io"
    stg "https://agent-creator.s.nannos.rcplus.io"
    prod "https://agent-creator.nannos.rcplus.io"
)

typeset -A ALLOY_URLS
ALLOY_URLS=(
    local "http://localhost:5004"
    dev "https://alloy-agent.d.nannos.rcplus.io"
    stg "https://alloy-agent.s.nannos.rcplus.io"
    prod "https://alloy-agent.nannos.rcplus.io"
)


typeset -A NAONOUS_MCP_URLS
NAONOUS_MCP_URLS=(
    local "http://localhost:8001/mcp"
    dev "https://naonous.d.alloy.rcplus.io/mcp"
    stg "https://naonous.s.alloy.rcplus.io/mcp"
    prod "https://naonous.alloy.rcplus.io/mcp"
)

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --backend)
            BACKEND_ENV="$2"
            shift 2
            ;;
        --orchestrator)
            ORCHESTRATOR_ENV="$2"
            shift 2
            ;;
        --agent-creator)
            AGENT_CREATOR_ENV="$2"
            shift 2
            ;;
        --alloy)
            ALLOY_ENV="$2"
            shift 2
            ;;
        --log-dir)
            LOG_DIR="$2"
            shift 2
            ;;
        -h|--help)
            HELP=true
            shift
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            HELP=true
            shift
            ;;
    esac
done

# Show help
if [ "$HELP" = true ]; then
    cat << EOF
Usage: ./start-dev.sh [OPTIONS]

Start local development environment with configurable remote services.

Options:
  --backend ENV           Backend environment (local|dev|stg|prod) [default: local]
  --orchestrator ENV      Orchestrator environment (local|dev|stg|prod) [default: local]
  --agent-creator ENV     Agent Creator environment (local|dev|stg|prod) [default: local]
  --alloy ENV            Alloy Agent environment (local|dev|stg|prod) [default: local]
  --log-dir DIR          Directory for log files [default: ./logs]
  -h, --help             Show this help message

Examples:
  # Run everything locally
  ./start-dev.sh

  # Run frontend locally, backend on dev, rest local
  ./start-dev.sh --backend dev

  # Run frontend locally, all backends on staging
  ./start-dev.sh --backend stg --orchestrator stg --agent-creator stg

  # Follow logs for a specific component
  tail -f logs/frontend.log
  tail -f logs/backend.log
  tail -f logs/orchestrator.log
  tail -f logs/agent-creator.log
  tail -f logs/alloy.log

Logs:
  Each component logs to its own file in the logs directory:
  - logs/frontend.log
  - logs/backend.log (if running locally)
  - logs/orchestrator.log (if running locally)
  - logs/agent-creator.log (if running locally)
  - logs/alloy.log (if running locally)

Stopping:
  Press Ctrl+C to stop all components
EOF
    exit 0
fi

# Validate environments
for env in "$BACKEND_ENV" "$ORCHESTRATOR_ENV" "$AGENT_CREATOR_ENV" "$ALLOY_ENV"; do
    if [[ ! "$env" =~ ^(local|dev|stg|prod)$ ]]; then
        echo -e "${RED}Error: Invalid environment '$env'. Must be one of: local, dev, stg, prod${NC}"
        exit 1
    fi
done

# Check if AWS_PROFILE is exported
if [ -z "$AWS_PROFILE" ]; then
    echo -e "${RED}Error: AWS_PROFILE environment variable is not set${NC}"
    echo -e "${YELLOW}Please export AWS_PROFILE before running this script${NC}"
    echo -e "${YELLOW}Example: export AWS_PROFILE=your-profile-name${NC}"
    exit 1
fi

echo -e "${GREEN}Using AWS Profile: ${AWS_PROFILE}${NC}\n"

# Create log directory
mkdir -p "$LOG_DIR"

# Array to track spawned process PIDs
typeset -a SPAWNED_PIDS

# Helper function to update or append environment variable in .env file
update_env_var() {
    local env_file="$1"
    local var_name="$2"
    local var_value="$3"
    
    # Check if variable exists in file
    if grep -q "^${var_name}=" "$env_file" 2>/dev/null; then
        # Update existing value (macOS compatible)
        sed -i '' "s|^${var_name}=.*|${var_name}=${var_value}|" "$env_file"
    else
        # Append new variable
        echo "${var_name}=${var_value}" >> "$env_file"
    fi
}

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}Shutting down components...${NC}"
    
    # Kill all tracked processes
    for pid in "${SPAWNED_PIDS[@]}"; do
        if ps -p "$pid" > /dev/null 2>&1; then
            echo -e "${YELLOW}Stopping process $pid...${NC}"
            kill "$pid" 2>/dev/null || true
        fi
    done
    
    # Give processes time to shut down gracefully
    sleep 2
    
    # Force kill if still running
    for pid in "${SPAWNED_PIDS[@]}"; do
        if ps -p "$pid" > /dev/null 2>&1; then
            echo -e "${YELLOW}Force killing process $pid...${NC}"
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    
    echo -e "${GREEN}All components stopped${NC}"
}

trap cleanup SIGINT SIGTERM

# Function to check if port is in use and offer to kill process
check_port() {
    local port=$1
    local service_name=$2
    
    # Check if port is in use (get all PIDs as array)
    local pids=($(lsof -ti:$port 2>/dev/null))
    
    if [ ${#pids[@]} -gt 0 ]; then
        echo -e "${YELLOW}Port ${port} (${service_name}) is already in use by process(es):${NC}"
        lsof -i:$port | grep LISTEN
        echo -e "${YELLOW}Do you want to kill these processes? [y/N]${NC}"
        read -r response
        
        if [[ "$response" =~ ^[Yy]$ ]]; then
            echo -e "${YELLOW}Killing processes on port ${port}...${NC}"
            # Kill all PIDs
            for pid in "${pids[@]}"; do
                kill -9 "$pid" 2>/dev/null || true
            done
            sleep 1
            
            # Verify port is now free
            if lsof -ti:$port > /dev/null 2>&1; then
                echo -e "${RED}Failed to free port ${port}${NC}"
                exit 1
            else
                echo -e "${GREEN}Port ${port} is now free${NC}\n"
            fi
        else
            echo -e "${RED}Cannot start ${service_name} while port ${port} is in use${NC}"
            exit 1
        fi
    fi
}

# Function to ensure ports are available
ensure_ports_available() {
    echo -e "${BLUE}Checking required ports...${NC}\n"
    
    # Check backend port if running locally
    if [ "$BACKEND_ENV" = "local" ]; then
        check_port 5001 "Backend"
    fi
    
    # Check orchestrator port if running locally
    if [ "$ORCHESTRATOR_ENV" = "local" ]; then
        check_port 10001 "Orchestrator"
    fi
    
    # Check agent creator port if running locally
    if [ "$AGENT_CREATOR_ENV" = "local" ]; then
        check_port 8080 "Agent Creator"
    fi
    
    # Check alloy port if running locally
    if [ "$ALLOY_ENV" = "local" ]; then
        check_port 5004 "Alloy"
    fi
    
    # Always check frontend port (always runs locally)
    check_port 5173 "Frontend"
    
    echo -e "${GREEN}All required ports are available${NC}\n"
}

# Function to ensure Docker database is running
ensure_docker_db() {
    echo -e "${YELLOW}Checking Docker database...${NC}"
    
    # Check if container is already running
    if docker ps --format '{{.Names}}' | grep -q '^playground-db$'; then
        echo -e "${GREEN}Database container is already running${NC}\n"
        return 0
    fi
    
    # Container doesn't exist or was stopped (--rm removes it), create and run it
    echo -e "${YELLOW}Starting database container...${NC}"
    if docker run --rm -d --name playground-db -p 5432:5432 nannos-infrastructure-agents-test-database:latest; then
        echo -e "${GREEN}Database container started${NC}\n"
        # Wait a moment for PostgreSQL to be ready
        sleep 3
        return 0
    else
        echo -e "${RED}Failed to start database container${NC}"
        echo -e "${RED}Make sure Docker is running and the image 'nannos-infrastructure-agents-test-database:latest' exists${NC}"
        exit 1
    fi
}

# Print configuration
echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}Development Environment Setup${NC}"
echo -e "${BLUE}================================${NC}"
echo -e "Frontend:        ${GREEN}local${NC} (always)"
echo -e "Backend:         ${GREEN}${BACKEND_ENV}${NC} (${BACKEND_URLS[$BACKEND_ENV]})"
echo -e "Orchestrator:    ${GREEN}${ORCHESTRATOR_ENV}${NC} (${ORCHESTRATOR_DOMAINS[$ORCHESTRATOR_ENV]})"
echo -e "Agent Creator:   ${GREEN}${AGENT_CREATOR_ENV}${NC} (${AGENT_CREATOR_URLS[$AGENT_CREATOR_ENV]})"
echo -e "Alloy Agent:    ${GREEN}${ALLOY_ENV}${NC} (${ALLOY_URLS[$ALLOY_ENV]})"
echo -e "Logs:            ${GREEN}${LOG_DIR}/${NC}"
echo -e "${BLUE}================================${NC}\n"

# Function to start a component
start_component() {
    local name=$1
    local working_dir=$2
    local command=$3
    local log_file="${LOG_DIR}/${name}.log"
    local abs_working_dir="${REPO_ROOT}/${working_dir}"
    local abs_log_file="${REPO_ROOT}/${log_file}"
    
    echo -e "${YELLOW}Starting ${name}...${NC}"
    echo -e "  Log: ${abs_log_file}"
    echo -e "  Dir: ${abs_working_dir}"
    echo -e "  Cmd: ${command}"
    
    # Create log file
    mkdir -p "$(dirname "$abs_log_file")"
    echo "=== Starting ${name} at $(date) ===" > "$abs_log_file"
    echo "Working directory: ${abs_working_dir}" >> "$abs_log_file"
    echo "Command: ${command}" >> "$abs_log_file"
    echo "===" >> "$abs_log_file"
    
    # Start component in background
    (
        cd "$abs_working_dir" || exit 1
        echo "Changed to directory: $(pwd)" >> "$abs_log_file"
        eval "$command"
    ) >> "$abs_log_file" 2>&1 &
    
    local pid=$!
    SPAWNED_PIDS+=($pid)
    
    # Give it a moment to start
    sleep 1
    
    # Check if process is still running
    if kill -0 $pid 2>/dev/null; then
        echo -e "${GREEN}  Started with PID ${pid}${NC}\n"
    else
        echo -e "${RED}  Failed to start. Log output:${NC}\n"
        cat "$abs_log_file"
        exit 1
    fi
}

# Check if required ports are available
ensure_ports_available

# Generate .env files and start components

# 1. BACKEND
if [ "$BACKEND_ENV" = "local" ]; then
    # Ensure Docker database is running for local backend
    ensure_docker_db
    
    echo -e "${YELLOW}Configuring Backend (local)...${NC}"
    pushd app/playground-backend > /dev/null
    
    # Generate .env from template
    cp .env.template .env
    
    # Backend runs locally with local PostgreSQL
    update_env_var ".env" "POSTGRES_HOST" "localhost"
    update_env_var ".env" "POSTGRES_PORT" "5432"
    update_env_var ".env" "POSTGRES_USER" "postgres"
    update_env_var ".env" "POSTGRES_PASSWORD" "password"
    
    # Set frontend URL (frontend always runs locally)
    update_env_var ".env" "FRONTEND_URL" "http://localhost:5173"
    
    # Configure orchestrator based on environment
    if [ "$ORCHESTRATOR_ENV" = "local" ]; then
        update_env_var ".env" "ORCHESTRATOR_CLIENT_ID" "orchestrator"
        update_env_var ".env" "ORCHESTRATOR_BASE_DOMAIN" "localhost:10001"
        update_env_var ".env" "ORCHESTRATOR_ENVIRONMENT" "local"
    else
        update_env_var ".env" "ORCHESTRATOR_CLIENT_ID" "orchestrator"
        update_env_var ".env" "ORCHESTRATOR_BASE_DOMAIN" "${ORCHESTRATOR_DOMAINS[$ORCHESTRATOR_ENV]}"
        update_env_var ".env" "ORCHESTRATOR_ENVIRONMENT" "$ORCHESTRATOR_ENV"
    fi
    
    # Override environment-specific AWS resources (always use dev for local)
    update_env_var ".env" "CHECKPOINT_DYNAMODB_TABLE_NAME" "dev-nannos-infrastructure-agents-langgraph-checkpoints"
    update_env_var ".env" "CHECKPOINT_S3_BUCKET_NAME" "dev-nannos-infrastructure-agents-orchestrator-checkpoints"
    update_env_var ".env" "DYNAMODB_USERS_TABLE" "dev-nannos-infrastructure-agents-users"
    update_env_var ".env" "LANGSMITH_PROJECT" "dev-nannos-agent-framework"
    
    # Fetch secrets from AWS SSM
    echo -e "${YELLOW}Fetching secrets from AWS SSM...${NC}"
    
    if ! AZURE_OPENAI_API_KEY=$(aws ssm get-parameter --name /nannos/openai-api-key-chatgpt-4o --output json --with-decryption | jq -r .Parameter.Value); then
        echo -e "${RED}Failed to fetch AZURE_OPENAI_API_KEY from AWS SSM${NC}"
        exit 1
    fi
    update_env_var ".env" "AZURE_OPENAI_API_KEY" "$AZURE_OPENAI_API_KEY"
    
    if ! LANGSMITH_API_KEY=$(aws ssm get-parameter --name /nannos/infrastructure-agents/langsmith-api-key --output json --with-decryption | jq -r .Parameter.Value); then
        echo -e "${RED}Failed to fetch LANGSMITH_API_KEY from AWS SSM${NC}"
        exit 1
    fi
    update_env_var ".env" "LANGSMITH_API_KEY" "$LANGSMITH_API_KEY"
    
    if ! OIDC_CLIENT_SECRET=$(aws ssm get-parameter --name /nannos/infrastructure-agents/oidc-chat-ui-client-secret --output json --with-decryption | jq -r .Parameter.Value); then
        echo -e "${RED}Failed to fetch OIDC_CLIENT_SECRET from AWS SSM${NC}"
        exit 1
    fi
    update_env_var ".env" "OIDC_CLIENT_SECRET" "$OIDC_CLIENT_SECRET"
    
    
    popd > /dev/null
    
    start_component "backend" "app/playground-backend" "uv run --env-file .env python app.py"
    
    # Wait for backend to be ready
    echo -e "${YELLOW}Waiting for backend to be ready...${NC}"
    for i in {1..30}; do
        if curl -s http://localhost:5001/api/v1/health > /dev/null 2>&1; then
            echo -e "${GREEN}Backend is ready!${NC}\n"
            break
        fi
        if [ $i -eq 30 ]; then
            echo -e "${RED}Backend failed to start. Check logs/${LOG_DIR}/backend.log${NC}"
            exit 1
        fi
        sleep 1
    done
fi

# 2. ORCHESTRATOR
if [ "$ORCHESTRATOR_ENV" = "local" ]; then
    echo -e "${YELLOW}Configuring Orchestrator (local)...${NC}"
    pushd app/orchestrator-agent > /dev/null
    
    # Generate .env from template
    cp .env.template .env
    
    # Generate .env from template
    cp .env.template .env
    
    # Configure playground backend URL based on backend environment
    if [ "$BACKEND_ENV" = "local" ]; then
        update_env_var ".env" "PLAYGROUND_BACKEND_URL" "http://localhost:5001"
    else
        update_env_var ".env" "PLAYGROUND_BACKEND_URL" "${BACKEND_URLS[$BACKEND_ENV]}"
    fi
    
    # Add configuration based on backend environment
    if [ "$BACKEND_ENV" = "local" ]; then        
        # Use local PostgreSQL
        update_env_var ".env" "POSTGRES_HOST" "localhost"
        update_env_var ".env" "POSTGRES_PORT" "5432"
        update_env_var ".env" "POSTGRES_USER" "postgres"
        update_env_var ".env" "POSTGRES_PASSWORD" "password"
        
        # Override environment-specific AWS resources (use dev for local)
        update_env_var ".env" "CHECKPOINT_DYNAMODB_TABLE_NAME" "dev-nannos-infrastructure-agents-langgraph-checkpoints"
        update_env_var ".env" "CHECKPOINT_S3_BUCKET_NAME" "dev-nannos-infrastructure-agents-orchestrator-checkpoints"
        update_env_var ".env" "DYNAMODB_USERS_TABLE" "dev-nannos-infrastructure-agents-users"
        update_env_var ".env" "LANGSMITH_PROJECT" "dev-nannos-agent-framework"
    else
        # Use remote backend's PostgreSQL and AWS resources
        case "$BACKEND_ENV" in
            dev) ENV_PREFIX="dev" ;;
            stg) ENV_PREFIX="stg" ;;
            prod) ENV_PREFIX="prod" ;;
        esac
        
        echo -e "${YELLOW}Fetching remote backend database configuration...${NC}"
        
        # Fetch RDS endpoint from CloudFormation stack output
        RDS_STACK_NAME="${ENV_PREFIX}-nannos-infrastructure-agents-rds"
        PG_HOST=$(aws cloudformation describe-stacks --stack-name "$RDS_STACK_NAME" --query 'Stacks[0].Outputs[?OutputKey==`RdsInstanceEndpointAddress`].OutputValue' --output text 2>/dev/null || echo "")
        
        if [ -n "$PG_HOST" ]; then
            update_env_var ".env" "POSTGRES_HOST" "${PG_HOST}"
            update_env_var ".env" "POSTGRES_PORT" "5432"
            
            # Fetch database credentials
            PG_USER=$(aws ssm get-parameter --name /nannos/infrastructure-agents/rds-docstore-username --output json --with-decryption | jq -r .Parameter.Value)
            PG_PASS=$(aws ssm get-parameter --name /nannos/infrastructure-agents/rds-docstore-password --output json --with-decryption | jq -r .Parameter.Value)
            
            update_env_var ".env" "POSTGRES_USER" "${PG_USER}"
            update_env_var ".env" "POSTGRES_PASSWORD" "${PG_PASS}"
        fi
        
        # Override environment-specific AWS resources based on backend env
        update_env_var ".env" "CHECKPOINT_DYNAMODB_TABLE_NAME" "${ENV_PREFIX}-nannos-infrastructure-agents-langgraph-checkpoints"
        update_env_var ".env" "CHECKPOINT_S3_BUCKET_NAME" "${ENV_PREFIX}-nannos-infrastructure-agents-orchestrator-checkpoints"
        update_env_var ".env" "DYNAMODB_USERS_TABLE" "${ENV_PREFIX}-nannos-infrastructure-agents-users"
        update_env_var ".env" "LANGSMITH_PROJECT" "${ENV_PREFIX}-nannos-agent-framework"
    fi
    
    # Fetch secrets from AWS SSM
    echo -e "${YELLOW}Fetching secrets from AWS SSM...${NC}"
    
    if ! AZURE_OPENAI_API_KEY=$(aws ssm get-parameter --name /nannos/openai-api-key-chatgpt-4o --output json --with-decryption | jq -r .Parameter.Value); then
        echo -e "${RED}Failed to fetch AZURE_OPENAI_API_KEY from AWS SSM${NC}"
        exit 1
    fi
    update_env_var ".env" "AZURE_OPENAI_API_KEY" "$AZURE_OPENAI_API_KEY"
    
    if ! LANGSMITH_API_KEY=$(aws ssm get-parameter --name /nannos/infrastructure-agents/langsmith-api-key --output json --with-decryption | jq -r .Parameter.Value); then
        echo -e "${RED}Failed to fetch LANGSMITH_API_KEY from AWS SSM${NC}"
        exit 1
    fi
    update_env_var ".env" "LANGSMITH_API_KEY" "$LANGSMITH_API_KEY"
    
    if ! OIDC_CLIENT_SECRET=$(aws ssm get-parameter --name /nannos/infrastructure-agents/oidc-orchestrator-client-secret --output json --with-decryption | jq -r .Parameter.Value); then
        echo -e "${RED}Failed to fetch OIDC_CLIENT_SECRET from AWS SSM${NC}"
        exit 1
    fi
    update_env_var ".env" "OIDC_CLIENT_SECRET" "$OIDC_CLIENT_SECRET"
    
    if ! GCP_KEY=$(aws ssm get-parameter --name /nannos/infrastructure-agents/gcp-key --output json --with-decryption | jq -r .Parameter.Value); then
        echo -e "${RED}Failed to fetch GCP_KEY from AWS SSM${NC}"
        exit 1
    fi
    update_env_var ".env" "GCP_KEY" "'$GCP_KEY'"
    
    popd > /dev/null
    
    start_component "orchestrator" "app/orchestrator-agent" "uv run --env-file .env python main.py --host localhost --reload"
    
    # Wait for orchestrator to be ready
    echo -e "${YELLOW}Waiting for orchestrator to be ready...${NC}"
    for i in {1..30}; do
        if curl -s http://localhost:10001/health > /dev/null 2>&1; then
            echo -e "${GREEN}Orchestrator is ready!${NC}\n"
            break
        fi
        if [ $i -eq 30 ]; then
            echo -e "${RED}Orchestrator failed to start. Check ${LOG_DIR}/orchestrator.log${NC}"
            exit 1
        fi
        sleep 1
    done
fi

# 3. AGENT CREATOR
if [ "$AGENT_CREATOR_ENV" = "local" ]; then
    echo -e "${YELLOW}Configuring Agent Creator (local)...${NC}"
    pushd app/agent-creator > /dev/null
    
    # Generate .env from template
    cp .env.template .env
    
    # Add configuration pointing to other services
    update_env_var ".env" "PLAYGROUND_BACKEND_URL" "${BACKEND_URLS[$BACKEND_ENV]}"
    update_env_var ".env" "PLAYGROUND_FRONTEND_URL" "http://localhost:5173"
    
    # Override environment-specific AWS resources (always use dev for local)
    update_env_var ".env" "CHECKPOINT_DYNAMODB_TABLE_NAME" "dev-nannos-infrastructure-agents-langgraph-checkpoints"
    update_env_var ".env" "CHECKPOINT_S3_BUCKET_NAME" "dev-nannos-infrastructure-agents-orchestrator-checkpoints"

    if ! OIDC_CLIENT_SECRET=$(aws ssm get-parameter --name /nannos/infrastructure-agents/agent-creator/oidc-client-secret --output json --with-decryption | jq -r .Parameter.Value); then
        echo -e "${RED}Failed to fetch OIDC_CLIENT_SECRET from AWS SSM${NC}"
        exit 1
    fi
    update_env_var ".env" "OIDC_CLIENT_SECRET" "$OIDC_CLIENT_SECRET"

    if ! LANGSMITH_API_KEY=$(aws ssm get-parameter --name /nannos/infrastructure-agents/langsmith-api-key --output json --with-decryption | jq -r .Parameter.Value); then
        echo -e "${RED}Failed to fetch LANGSMITH_API_KEY from AWS SSM${NC}"
        exit 1
    fi
    update_env_var ".env" "LANGSMITH_API_KEY" "$LANGSMITH_API_KEY"

    popd > /dev/null
    
    start_component "agent-creator" "app/agent-creator" "uv run --env-file .env python main.py --reload"
    
    # Wait for agent-creator to be ready
    echo -e "${YELLOW}Waiting for agent-creator to be ready...${NC}"
    for i in {1..30}; do
        if curl -s http://localhost:8080/health > /dev/null 2>&1; then
            echo -e "${GREEN}Agent Creator is ready!${NC}\n"
            break
        fi
        if [ $i -eq 30 ]; then
            echo -e "${RED}Agent Creator failed to start. Check ${LOG_DIR}/agent-creator.log${NC}"
            exit 1
        fi
        sleep 1
    done
fi

# 4. ALLOY AGENT
if [ "$ALLOY_ENV" = "local" ]; then
    echo -e "${YELLOW}Configuring Alloy Agent (local)...${NC}"
    pushd app/alloy-agent > /dev/null
    
    # Generate .env from template
    cp .env.template .env
    
    # Override environment-specific AWS resources (always use dev for local)
    update_env_var ".env" "CHECKPOINT_DYNAMODB_TABLE_NAME" "dev-nannos-infrastructure-agents-langgraph-checkpoints"
    update_env_var ".env" "CHECKPOINT_S3_BUCKET_NAME" "dev-nannos-infrastructure-agents-orchestrator-checkpoints"
    
    # Set MCP URLs based on ALLOY_ENV (defaults to dev for local)
    update_env_var ".env" "NAONOUS_MCP_URL" "${NAONOUS_MCP_URLS[$ALLOY_ENV]}"
    
    # Configure cost tracking backend URL based on backend environment
    update_env_var ".env" "PLAYGROUND_BACKEND_URL" "${BACKEND_URLS[$BACKEND_ENV]}"
    update_env_var ".env" "PLAYGROUND_FRONTEND_URL" "http://localhost:5173"

    if ! LANGSMITH_API_KEY=$(aws ssm get-parameter --name /nannos/infrastructure-agents/langsmith-api-key --output json --with-decryption | jq -r .Parameter.Value); then
        echo -e "${RED}Failed to fetch LANGSMITH_API_KEY from AWS SSM${NC}"
        exit 1
    fi
    update_env_var ".env" "LANGSMITH_API_KEY" "$LANGSMITH_API_KEY"

    popd > /dev/null
    
    start_component "alloy" "app/alloy-agent" "uv run --env-file .env python main.py"
    
    # Wait for alloy-agent to be ready
    echo -e "${YELLOW}Waiting for alloy-agent to be ready...${NC}"
    for i in {1..30}; do
        if curl -s http://localhost:5004/health > /dev/null 2>&1; then
            echo -e "${GREEN}Alloy Agent is ready!${NC}\n"
            break
        fi
        if [ $i -eq 30 ]; then
            echo -e "${RED}Alloy Agent failed to start. Check ${LOG_DIR}/alloy.log${NC}"
            exit 1
        fi
        sleep 1
    done
fi

# 5. FRONTEND (always local)
echo -e "${YELLOW}Configuring Frontend (local)...${NC}"
pushd app/playground-frontend > /dev/null

# Determine environment prefix for SSM paths
case "$BACKEND_ENV" in
    dev) ENV_PREFIX="dev" ;;
    stg) ENV_PREFIX="stg" ;;
    prod) ENV_PREFIX="prod" ;;
    *) ENV_PREFIX="dev" ;;
esac

# Create .env file for frontend
cat > .env << EOF
# Generated by start-dev.sh
VITE_API_BASE_URL=${BACKEND_URLS[$BACKEND_ENV]}
VITE_ORCHESTRATOR_BASE_DOMAIN=${ORCHESTRATOR_DOMAINS[$ORCHESTRATOR_ENV]}
VITE_LANGSMITH_ORGANIZATION_ID=eacaca37-6472-40d5-80b4-9206d058caef
VITE_LANGSMITH_PROJECT_ID=052ac0c2-787f-44e2-810a-34d8b7845a09
EOF

# If backend is remote, fetch its PostgreSQL configuration for SDK generation
if [ "$BACKEND_ENV" != "local" ]; then
    echo -e "${YELLOW}Fetching remote backend database configuration...${NC}"
    
    # Fetch RDS endpoint from CloudFormation stack output
    RDS_STACK_NAME="${ENV_PREFIX}-nannos-infrastructure-agents-rds"
    PG_HOST=$(aws cloudformation describe-stacks --stack-name "$RDS_STACK_NAME" --query 'Stacks[0].Outputs[?OutputKey==`RdsInstanceEndpointAddress`].OutputValue' --output text 2>/dev/null || echo "")
    
    if [ -n "$PG_HOST" ]; then
        update_env_var ".env" "POSTGRES_HOST" "${PG_HOST}"
        update_env_var ".env" "POSTGRES_PORT" "5432"
        
        # Fetch database credentials
        PG_USER=$(aws ssm get-parameter --name /nannos/infrastructure-agents/rds-service-username --output json --with-decryption | jq -r .Parameter.Value)
        PG_PASS=$(aws ssm get-parameter --name /nannos/infrastructure-agents/rds-service-password --output json --with-decryption | jq -r .Parameter.Value)
        
        update_env_var ".env" "POSTGRES_USER" "${PG_USER}"
        update_env_var ".env" "POSTGRES_PASSWORD" "${PG_PASS}"
    fi
fi

# Set OVERRIDE_URL for SDK generation
export OVERRIDE_URL="${BACKEND_URLS[$BACKEND_ENV]}/api/v1/openapi.json"

popd > /dev/null

start_component "frontend" "app/playground-frontend" "npm run dev"

# Wait for frontend to be ready
echo -e "${YELLOW}Waiting for frontend to be ready...${NC}"
for i in {1..30}; do
    if curl -s http://localhost:5173 > /dev/null 2>&1; then
        echo -e "${GREEN}Frontend is ready!${NC}\n"
        break
    fi
    if [ $i -eq 30 ]; then
        echo -e "${RED}Frontend failed to start. Check ${LOG_DIR}/frontend.log${NC}"
        exit 1
    fi
    sleep 1
done

# Print final status
echo -e "${BLUE}================================${NC}"
echo -e "${GREEN}✓ All components started!${NC}"
echo -e "${BLUE}================================${NC}"
echo -e "Frontend:        ${GREEN}http://localhost:5173${NC}"
if [ "$BACKEND_ENV" = "local" ]; then
    echo -e "Backend:         ${GREEN}http://localhost:5001${NC}"
else
    echo -e "Backend:         ${GREEN}${BACKEND_URLS[$BACKEND_ENV]}${NC} (remote)"
fi
if [ "$ORCHESTRATOR_ENV" = "local" ]; then
    echo -e "Orchestrator:    ${GREEN}http://localhost:10001${NC}"
else
    echo -e "Orchestrator:    ${GREEN}https://${ORCHESTRATOR_DOMAINS[$ORCHESTRATOR_ENV]}${NC} (remote)"
fi
if [ "$AGENT_CREATOR_ENV" = "local" ]; then
    echo -e "Agent Creator:   ${GREEN}http://localhost:8080${NC}"
else
    echo -e "Agent Creator:   ${GREEN}${AGENT_CREATOR_URLS[$AGENT_CREATOR_ENV]}${NC} (remote)"
fi
if [ "$ALLOY_ENV" = "local" ]; then
    echo -e "Alloy Agent:    ${GREEN}http://localhost:5004${NC}"
else
    echo -e "Alloy Agent:    ${GREEN}${ALLOY_URLS[$ALLOY_ENV]}${NC} (remote)"
fi
echo -e "${BLUE}================================${NC}"
echo -e "\n${YELLOW}Follow logs:${NC}"
if [ "$BACKEND_ENV" = "local" ]; then
    echo -e "  tail -f ${LOG_DIR}/backend.log"
fi
if [ "$ORCHESTRATOR_ENV" = "local" ]; then
    echo -e "  tail -f ${LOG_DIR}/orchestrator.log"
fi
if [ "$AGENT_CREATOR_ENV" = "local" ]; then
    echo -e "  tail -f ${LOG_DIR}/agent-creator.log"
fi
if [ "$ALLOY_ENV" = "local" ]; then
    echo -e "  tail -f ${LOG_DIR}/alloy.log"
fi
echo -e "  tail -f ${LOG_DIR}/frontend.log"
echo -e "\n${YELLOW}Press Ctrl+C to stop all components${NC}\n"

# Wait for all background processes
wait
