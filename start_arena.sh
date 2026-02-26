#!/bin/bash
#
# Trading Arena Startup Script (Bash Version)
# Simpler alternative to start_arena.py for users who prefer shell scripts
#
# Usage:
#   ./start_arena.sh
#   ./start_arena.sh --cloud-broker broker.example.com:9092
#   ./start_arena.sh --with-viewer
#   OPENAI_API_KEY=sk-... ./start_arena.sh
#

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
DIM='\033[90m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Defaults
BROKER_URL="${KAFKA_BOOTSTRAP_SERVERS:-localhost:9092}"
API_KEY="${OPENAI_API_KEY:-}"
MODEL_ID="gpt-4o-mini"
INTERVAL=60
WITH_VIEWER=false
CLOUD_BROKER=""
SKIP_CHECKS=false

# Process list for cleanup
PIDS=""

# Logging
log() {
    local level="$1"
    local message="$2"
    local timestamp=$(date '+%H:%M:%S')
    local color="$CYAN"

    case "$level" in
        "info") color="$CYAN" ;;
        "success") color="$GREEN" ;;
        "warning") color="$YELLOW" ;;
        "error") color="$RED" ;;
        "header") color="$MAGENTA$BOLD" ;;
    esac

    if [ "$level" = "header" ]; then
        echo -e "\n${color}============================================================${NC}"
        echo -e "${color}${message}${NC}"
        echo -e "${color}============================================================${NC}"
    else
        echo -e "${DIM}[${timestamp}]${NC} ${color}${message}${NC}"
    fi
}

# Cleanup function
cleanup() {
    log "header" "Shutting Down"
    if [ -n "$PIDS" ]; then
        for pid in $PIDS; do
            if kill -0 "$pid" 2>/dev/null; then
                log "info" "Stopping process $pid..."
                kill -TERM "$pid" 2>/dev/null || true
                sleep 2
                kill -KILL "$pid" 2>/dev/null || true
            fi
        done
    fi

    # Stop Docker broker if we started it
    if [ -z "$CLOUD_BROKER" ] && [ -d "../calfkit-broker" ]; then
        log "info" "Stopping Kafka broker..."
        (cd ../calfkit-broker && make dev-down 2>/dev/null || true)
    fi

    log "success" "Shutdown complete"
    exit 0
}

# Trap signals
trap cleanup SIGINT SIGTERM EXIT

# Help
show_help() {
    cat << EOF
Trading Arena Startup Script

Usage: $0 [OPTIONS]

Options:
    --broker-url URL        Kafka broker URL (default: localhost:9092)
    --cloud-broker URL      Use cloud broker instead of local
    --api-key KEY           API key for LLM (default: \$OPENAI_API_KEY)
    --model-id ID           Model ID (default: gpt-4o-mini)
    --interval SECONDS      Market data interval (default: 60)
    --with-viewer           Also start response viewer
    --skip-checks           Skip prerequisite checks
    -h, --help              Show this help

Examples:
    # Basic startup
    $0

    # Use cloud broker
    $0 --cloud-broker broker.example.com:9092

    # Fast updates with viewer
    $0 --interval 30 --with-viewer

    # Specify API key
    OPENAI_API_KEY=sk-... $0
EOF
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --broker-url)
            BROKER_URL="$2"
            shift 2
            ;;
        --cloud-broker)
            CLOUD_BROKER="$2"
            BROKER_URL="$2"
            shift 2
            ;;
        --api-key)
            API_KEY="$2"
            shift 2
            ;;
        --model-id)
            MODEL_ID="$2"
            shift 2
            ;;
        --interval)
            INTERVAL="$2"
            shift 2
            ;;
        --with-viewer)
            WITH_VIEWER=true
            shift
            ;;
        --skip-checks)
            SKIP_CHECKS=true
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            log "error" "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

# Check prerequisites
check_prerequisites() {
    log "header" "Checking Prerequisites"

    local all_good=true

    # Check Python
    if command -v python3 &> /dev/null; then
        local py_version=$(python3 --version 2>&1 | cut -d' ' -f2)
        log "success" "Python: $py_version"
    else
        log "error" "Python 3 not found"
        all_good=false
    fi

    # Check uv
    if command -v uv &> /dev/null; then
        local uv_version=$(uv --version 2>&1)
        log "success" "uv: $uv_version"
    else
        log "error" "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
        all_good=false
    fi

    # Check Docker
    if command -v docker &> /dev/null; then
        local docker_version=$(docker --version 2>&1)
        log "success" "Docker: $docker_version"

        if docker info &> /dev/null; then
            log "success" "Docker daemon is running"
        else
            log "error" "Docker daemon is not running"
            all_good=false
        fi
    else
        log "error" "Docker not found"
        all_good=false
    fi

    # Check API key
    if [ -n "$API_KEY" ]; then
        local masked="${API_KEY:0:8}...${API_KEY: -4}"
        log "success" "API key: $masked"
    else
        log "warning" "No API key set. Set OPENAI_API_KEY or use --api-key"
    fi

    if [ "$all_good" = false ] && [ "$SKIP_CHECKS" = false ]; then
        log "error" "Prerequisite checks failed. Use --skip-checks to bypass."
        exit 1
    fi
}

# Setup broker
setup_broker() {
    if [ -n "$CLOUD_BROKER" ]; then
        log "info" "Using cloud broker: $CLOUD_BROKER"
        return 0
    fi

    log "header" "Setting up Kafka Broker"

    local broker_dir="../calfkit-broker"

    # Clone if needed
    if [ ! -d "$broker_dir" ]; then
        log "warning" "calfkit-broker not found, cloning..."
        (cd .. && git clone https://github.com/calf-ai/calfkit-broker) || {
            log "error" "Failed to clone calfkit-broker"
            exit 1
        }
    fi

    # Check if already running
    if docker ps --filter "name=kafka" --format "{{.Names}}" | grep -q kafka; then
        log "success" "Kafka broker already running"
        return 0
    fi

    # Start broker
    log "info" "Starting Kafka broker with docker-compose..."
    (cd "$broker_dir" && make dev-up) &
    local broker_pid=$!
    PIDS="$PIDS $broker_pid"

    log "info" "Waiting for broker to be ready (30-60s)..."
    sleep 15

    # Wait for healthy status
    for i in {1..30}; do
        if docker ps --filter "name=kafka" --format "{{.Status}}" | grep -q "healthy\|Up"; then
            log "success" "Kafka broker is ready!"
            return 0
        fi
        sleep 2
    done

    log "warning" "Broker may still be starting, continuing..."
}

# Install dependencies
install_deps() {
    log "header" "Installing Dependencies"
    uv sync || {
        log "error" "Failed to install dependencies"
        exit 1
    }
    log "success" "Dependencies installed"
}

# Start a component
start_component() {
    local name="$1"
    shift
    local cmd="$@"

    log "info" "Starting $name..."

    # Create logs directory
    mkdir -p logs

    # Start process
    KAFKA_BOOTSTRAP_SERVERS="$BROKER_URL" \
        eval "$cmd" > "logs/${name}.log" 2>&1 &

    local pid=$!
    PIDS="$PIDS $pid"

    log "success" "Started $name (PID: $pid)"
    sleep 2
}

# Main startup
main() {
    log "header" "Trading Arena Startup"

    # Phase 1: Prerequisites
    if [ "$SKIP_CHECKS" = false ]; then
        check_prerequisites
    fi

    # Phase 2: Setup
    mkdir -p logs

    # Phase 3: Broker
    setup_broker

    # Phase 4: Dependencies
    install_deps

    # Phase 5: Components
    sleep 2

    # Coinbase connector
    start_component "coinbase-connector" \
        "uv run python coinbase_connector.py --bootstrap-servers $BROKER_URL --interval $INTERVAL"

    # Tools & Dashboard
    start_component "tools-dashboard" \
        "uv run python tools_and_dashboard.py --bootstrap-servers $BROKER_URL"

    sleep 2

    # ChatNode (if API key available)
    if [ -n "$API_KEY" ]; then
        start_component "chat-node" \
            "uv run python deploy_chat_node.py --name default-chat --model-id $MODEL_ID --bootstrap-servers $BROKER_URL --api-key $API_KEY"

        sleep 3

        # Agent routers
        log "header" "Starting Agent Routers"

        start_component "agent-momentum" \
            "uv run python deploy_router_node.py --name momentum-trader --chat-node-name default-chat --strategy momentum --bootstrap-servers $BROKER_URL"

        start_component "agent-brainrot" \
            "uv run python deploy_router_node.py --name brainrot-trader --chat-node-name default-chat --strategy brainrot --bootstrap-servers $BROKER_URL"

        start_component "agent-scalper" \
            "uv run python deploy_router_node.py --name scalper-trader --chat-node-name default-chat --strategy scalper --bootstrap-servers $BROKER_URL"
    else
        log "warning" "No API key - skipping ChatNode and agents"
    fi

    # Response viewer (optional)
    if [ "$WITH_VIEWER" = true ]; then
        start_component "response-viewer" \
            "uv run python response_viewer.py --bootstrap-servers $BROKER_URL"
    fi

    # Monitor
    log "header" "All Components Started!"
    log "info" "Dashboard: tools-dashboard is running"
    log "info" "Logs: ./logs/"
    log "info" "Press Ctrl+C to stop all components"

    echo -e "\n${MAGENTA}${BOLD}Active Processes:${NC}"
    for pid in $PIDS; do
        if kill -0 "$pid" 2>/dev/null; then
            local pname=$(ps -o comm= -p "$pid" 2>/dev/null || echo "unknown")
            echo -e "  ${GREEN}●${NC} $pname (PID: $pid)"
        fi
    done
    echo ""

    # Wait forever
    while true; do
        sleep 1
    done
}

# Run main
main "$@"
