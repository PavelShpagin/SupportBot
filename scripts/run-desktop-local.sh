#!/bin/bash
# ==============================================================================
# Run signal-desktop locally on x86 machine
# ==============================================================================
# This script runs signal-desktop container locally (requires x86_64 machine)
# and exposes it via SSH reverse tunnel to the Oracle ARM VM.
#
# Usage:
#   ./scripts/run-desktop-local.sh [start|stop|tunnel|status]
#
# The Oracle VM's signal-ingest will connect to this local instance.
# ==============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Load environment
source .env 2>/dev/null || true

VM_IP="${ORACLE_VM_IP:-161.33.64.115}"
VM_KEY="${ORACLE_VM_KEY:-~/.ssh/supportbot_ed25519}"
VM_KEY="${VM_KEY/#\~/$HOME}"
VM_USER="opc"

CONTAINER_NAME="supportbot-desktop-local"
LOCAL_PORT=8001
REMOTE_PORT=8001

check_arch() {
    ARCH=$(uname -m)
    if [ "$ARCH" != "x86_64" ]; then
        log_error "This script requires x86_64 architecture. Current: $ARCH"
        log_error "Signal Desktop only works on x86_64."
        exit 1
    fi
    log_success "Architecture check passed: $ARCH"
}

cmd_start() {
    log_info "Starting signal-desktop locally..."
    check_arch
    
    # Create data directory
    mkdir -p /var/lib/signal/desktop-local
    
    # Build and run the container
    log_info "Building signal-desktop container..."
    docker build -t supportbot-signal-desktop:local -f signal-desktop/Dockerfile .
    
    # Stop existing container if running
    docker stop $CONTAINER_NAME 2>/dev/null || true
    docker rm $CONTAINER_NAME 2>/dev/null || true
    
    log_info "Starting container..."
    docker run -d \
        --name $CONTAINER_NAME \
        --shm-size=2gb \
        -p $LOCAL_PORT:8001 \
        -v /var/lib/signal/desktop-local:/home/signal/.config/Signal \
        -e SIGNAL_DATA_DIR=/home/signal/.config/Signal \
        supportbot-signal-desktop:local
    
    log_success "signal-desktop running locally on port $LOCAL_PORT"
    log_info "Test it: curl http://localhost:$LOCAL_PORT/healthz"
    echo ""
    log_info "Next: Run './scripts/run-desktop-local.sh tunnel' to expose to Oracle VM"
}

cmd_stop() {
    log_info "Stopping signal-desktop..."
    docker stop $CONTAINER_NAME 2>/dev/null || true
    docker rm $CONTAINER_NAME 2>/dev/null || true
    log_success "Stopped"
}

cmd_tunnel() {
    log_info "Setting up SSH reverse tunnel to Oracle VM..."
    log_info "This will expose local port $LOCAL_PORT as localhost:$REMOTE_PORT on the VM"
    
    if [ ! -f "$VM_KEY" ]; then
        log_error "SSH key not found: $VM_KEY"
        exit 1
    fi
    
    # Check if container is running
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log_error "Container $CONTAINER_NAME is not running. Start it first."
        exit 1
    fi
    
    log_warn "Keep this terminal open. Press Ctrl+C to stop the tunnel."
    echo ""
    
    # Create reverse tunnel: VM's localhost:8001 -> local machine's localhost:8001
    ssh -i "$VM_KEY" -o StrictHostKeyChecking=no \
        -R $REMOTE_PORT:localhost:$LOCAL_PORT \
        -N \
        "$VM_USER@$VM_IP"
}

cmd_status() {
    log_info "Local container status:"
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        docker ps --filter "name=$CONTAINER_NAME" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
        echo ""
        log_info "Testing local endpoint..."
        curl -s http://localhost:$LOCAL_PORT/healthz && echo " OK" || echo " FAILED"
    else
        log_warn "Container $CONTAINER_NAME is not running"
    fi
}

cmd_logs() {
    docker logs -f $CONTAINER_NAME
}

# ==============================================================================
# Main
# ==============================================================================

case "${1:-}" in
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    tunnel)
        cmd_tunnel
        ;;
    status)
        cmd_status
        ;;
    logs)
        cmd_logs
        ;;
    *)
        echo "Signal Desktop Local Runner"
        echo ""
        echo "Usage: $0 <command>"
        echo ""
        echo "Commands:"
        echo "  start   - Build and start signal-desktop container locally"
        echo "  stop    - Stop the local container"
        echo "  tunnel  - Create SSH tunnel to Oracle VM (keeps running)"
        echo "  status  - Check container status"
        echo "  logs    - View container logs"
        echo ""
        echo "Workflow:"
        echo "  1. ./scripts/run-desktop-local.sh start"
        echo "  2. ./scripts/run-desktop-local.sh tunnel  (keep this terminal open)"
        echo "  3. Update Oracle VM's .env: SIGNAL_DESKTOP_URL=http://localhost:8001"
        echo "  4. Restart signal-ingest on VM"
        ;;
esac
