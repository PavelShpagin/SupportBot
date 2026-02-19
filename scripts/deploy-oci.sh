#!/bin/bash
# ==============================================================================
# SupportBot OCI Deployment Script
# ==============================================================================
# Deploys SupportBot to Oracle Cloud Infrastructure VM
#
# Usage:
#   ./scripts/deploy-oci.sh [init|push|ssh|full]
#
# Requirements:
#   - OCI CLI configured
#   - SSH key for VM access
#   - .env file configured with ORACLE_VM_IP and ORACLE_VM_KEY
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

VM_IP="${ORACLE_VM_IP:-}"
VM_KEY="${ORACLE_VM_KEY:-~/.ssh/supportbot_ed25519}"
VM_USER="opc"
REMOTE_DIR="/home/opc/supportbot"

# Auto-detect user if opc doesn't work
detect_user() {
    if ssh -i "$VM_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 "opc@$VM_IP" "true" 2>/dev/null; then
        echo "opc"
    elif ssh -i "$VM_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 "ubuntu@$VM_IP" "true" 2>/dev/null; then
        echo "ubuntu"
    else
        echo "opc"
    fi
}

# Expand ~ in VM_KEY
VM_KEY="${VM_KEY/#\~/$HOME}"

check_prerequisites() {
    if [ -z "$VM_IP" ]; then
        log_error "ORACLE_VM_IP not set in .env"
        log_info "Set it to your OCI VM's public IP address"
        exit 1
    fi
    
    if [ ! -f "$VM_KEY" ]; then
        log_error "SSH key not found: $VM_KEY"
        log_info "Set ORACLE_VM_KEY in .env to your SSH private key path"
        exit 1
    fi
    
    log_success "Prerequisites OK: VM=$VM_IP, Key=$VM_KEY"
}

ssh_cmd() {
    ssh -i "$VM_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$VM_USER@$VM_IP" "$@"
}

scp_cmd() {
    scp -i "$VM_KEY" -o StrictHostKeyChecking=no "$@"
}

sync_cmd() {
    rsync -avz --progress \
        -e "ssh -i $VM_KEY -o StrictHostKeyChecking=no" \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.pytest_cache' \
        --exclude='test/data' \
        --exclude='reports' \
        --exclude='legacy' \
        --exclude='*.bak*' \
        --exclude='*.log' \
        --exclude='paper*' \
        "$@"
}

cmd_init() {
    log_info "Initializing OCI VM for SupportBot..."
    check_prerequisites
    
    log_info "Installing Docker and dependencies on VM..."
    ssh_cmd << 'ENDSSH'
set -e

# Update and install Docker
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin curl git

# Start Docker
sudo systemctl enable --now docker

# Add ubuntu user to docker group
sudo usermod -aG docker ubuntu

    # Create directories
    sudo mkdir -p /var/lib/signal/bot /var/lib/signal/ingest /var/lib/signal/desktop /var/lib/history
    
    # Set permissions for current user
    TARGET_USER=$(whoami)
    sudo chown -R $TARGET_USER:$TARGET_USER /var/lib/signal /var/lib/history
    chmod 755 /var/lib/signal /var/lib/signal/bot /var/lib/signal/ingest /var/lib/signal/desktop /var/lib/history

echo "Docker installed and configured!"
docker --version
docker compose version
ENDSSH
    
    log_success "VM initialized! You may need to reconnect for docker group to take effect."
}

cmd_push() {
    log_info "Pushing code to OCI VM..."
    check_prerequisites
    
    # Create remote directory
    ssh_cmd "mkdir -p $REMOTE_DIR"
    
    # Sync project files
    log_info "Syncing project files..."
    sync_cmd "$PROJECT_ROOT/" "$VM_USER@$VM_IP:$REMOTE_DIR/"
    
    log_success "Code pushed to $VM_IP:$REMOTE_DIR"
}

cmd_deploy_remote() {
    log_info "Building and deploying on remote VM..."
    check_prerequisites
    
    ssh_cmd << ENDSSH
set -e
cd $REMOTE_DIR

# Ensure directories exist
sudo mkdir -p /var/lib/signal/bot /var/lib/signal/ingest /var/lib/signal/desktop /var/lib/history
sudo chown -R \$(whoami):\$(whoami) /var/lib/signal /var/lib/history

echo "Pulling latest Docker images..."
docker compose -f docker-compose.prod.yml pull db rag redis || true

echo "Building and starting services..."
docker compose -f docker-compose.prod.yml up -d --build

echo "Waiting for services..."
sleep 15

echo "Service status:"
docker compose -f docker-compose.prod.yml ps

echo "Health check:"
curl -sf http://localhost:8000/healthz && echo " - API OK" || echo " - API FAILED"
ENDSSH
    
    log_success "Deployment complete on $VM_IP"
}

cmd_ssh() {
    check_prerequisites
    log_info "Connecting to OCI VM..."
    ssh -i "$VM_KEY" -o StrictHostKeyChecking=no "$VM_USER@$VM_IP"
}

cmd_logs() {
    check_prerequisites
    SERVICE="${1:-}"
    ssh_cmd "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml logs -f $SERVICE"
}

cmd_status() {
    check_prerequisites
    ssh_cmd "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml ps"
}

cmd_link_signal() {
    log_info "Linking Signal on remote VM..."
    check_prerequisites
    
    source .env
    
    log_warn "This will show a QR code link. Scan it with Signal on your phone."
    log_info "Go to: Signal -> Settings -> Linked Devices -> Link New Device"
    echo ""
    
    ssh_cmd "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml exec signal-bot signal-cli link -n 'SupportBot Server'"
}

cmd_set_avatar_remote() {
    log_info "Setting bot avatar on remote..."
    check_prerequisites
    
    source .env
    
    # Copy logo to remote
    scp_cmd "$PROJECT_ROOT/supportbot-logo.png" "$VM_USER@$VM_IP:$REMOTE_DIR/"
    
    ssh_cmd << ENDSSH
cd $REMOTE_DIR
docker cp supportbot-logo.png supportbot-api:/tmp/avatar.png
docker compose -f docker-compose.prod.yml exec signal-bot signal-cli -a $SIGNAL_BOT_E164 updateProfile --avatar /tmp/avatar.png --name "SupportBot"
ENDSSH
    
    log_success "Avatar set!"
}

cmd_full() {
    log_info "Full deployment to OCI..."
    
    cmd_push
    cmd_deploy_remote
    
    echo ""
    log_success "Full deployment complete!"
    echo ""
    log_info "Next steps:"
    echo "  1. Link Signal: ./scripts/deploy-oci.sh link-signal"
    echo "  2. Set avatar:  ./scripts/deploy-oci.sh set-avatar"
    echo "  3. Check logs:  ./scripts/deploy-oci.sh logs"
    echo ""
    log_info "API endpoint: http://$VM_IP:8000"
}

cmd_stop() {
    check_prerequisites
    ssh_cmd "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml down"
    log_success "Services stopped on remote."
}

cmd_restart() {
    check_prerequisites
    ssh_cmd "cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml restart"
    log_success "Services restarted on remote."
}

# ==============================================================================
# Main
# ==============================================================================

case "${1:-}" in
    init)
        cmd_init
        ;;
    push)
        cmd_push
        ;;
    deploy)
        cmd_deploy_remote
        ;;
    full)
        cmd_full
        ;;
    ssh)
        cmd_ssh
        ;;
    logs)
        cmd_logs "${2:-}"
        ;;
    status)
        cmd_status
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_restart
        ;;
    link-signal)
        cmd_link_signal
        ;;
    set-avatar)
        cmd_set_avatar_remote
        ;;
    *)
        echo "SupportBot OCI Deployment"
        echo ""
        echo "Usage: $0 <command>"
        echo ""
        echo "Commands:"
        echo "  init        - Initialize VM (install Docker, create dirs)"
        echo "  push        - Push code to VM"
        echo "  deploy      - Build and start services on VM"
        echo "  full        - Push + Deploy (complete deployment)"
        echo "  ssh         - SSH into VM"
        echo "  logs [svc]  - View logs"
        echo "  status      - Show service status"
        echo "  stop        - Stop services"
        echo "  restart     - Restart services"
        echo "  link-signal - Link Signal account"
        echo "  set-avatar  - Set bot avatar"
        echo ""
        echo "Environment (from .env):"
        echo "  ORACLE_VM_IP=$VM_IP"
        echo "  ORACLE_VM_KEY=$VM_KEY"
        ;;
esac
