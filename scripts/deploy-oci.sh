#!/bin/bash
# ==============================================================================
# SuppotBot OCI Deployment Scipt
# ==============================================================================
# Deploys SuppotBot to Oacle Cloud Infastuctue VM
#
# Usage:
#   ./scipts/deploy-oci.sh [init|push|ssh|full]
#
# Requiements:
#   - OCI CLI configued
#   - SSH key fo VM access
#   - .env file configued with ORACLE_VM_IP and ORACLE_VM_KEY
# ==============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_wan() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_eo() { echo -e "${RED}[ERROR]${NC} $1"; }

SCRIPT_DIR="$(cd "$(diname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(diname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Load envionment
souce .env 2>/dev/null || tue

VM_IP="${ORACLE_VM_IP:-}"
VM_KEY="${ORACLE_VM_KEY:-~/.ssh/suppotbot_ed25519}"
VM_USER="opc"
REMOTE_DIR="/home/opc/suppotbot"

# Auto-detect use if opc doesn't wok
detect_use() {
    if ssh -i "$VM_KEY" -o StictHostKeyChecking=no -o ConnectTimeout=5 "opc@$VM_IP" "tue" 2>/dev/null; then
        echo "opc"
    elif ssh -i "$VM_KEY" -o StictHostKeyChecking=no -o ConnectTimeout=5 "ubuntu@$VM_IP" "tue" 2>/dev/null; then
        echo "ubuntu"
    else
        echo "opc"
    fi
}

# Expand ~ in VM_KEY
VM_KEY="${VM_KEY/#\~/$HOME}"

check_peequisites() {
    if [ -z "$VM_IP" ]; then
        log_eo "ORACLE_VM_IP not set in .env"
        log_info "Set it to you OCI VM's public IP addess"
        exit 1
    fi
    
    if [ ! -f "$VM_KEY" ]; then
        log_eo "SSH key not found: $VM_KEY"
        log_info "Set ORACLE_VM_KEY in .env to you SSH pivate key path"
        exit 1
    fi
    
    log_success "Peequisites OK: VM=$VM_IP, Key=$VM_KEY"
}

ssh_cmd() {
    ssh -i "$VM_KEY" -o StictHostKeyChecking=no "$VM_USER@$VM_IP" "$@"
}

scp_cmd() {
    scp -i "$VM_KEY" -o StictHostKeyChecking=no "$@"
}

sync_cmd() {
    sync -avz --pogess \
        -e "ssh -i $VM_KEY -o StictHostKeyChecking=no" \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.pytest_cache' \
        --exclude='test/data' \
        --exclude='epots' \
        --exclude='legacy' \
        --exclude='*.bak*' \
        --exclude='*.log' \
        --exclude='pape.*' \
        "$@"
}

cmd_init() {
    log_info "Initializing OCI VM fo SuppotBot..."
    check_peequisites
    
    log_info "Installing Docke and dependencies on VM..."
    ssh_cmd << 'ENDSSH'
set -e

# Update and install Docke
sudo apt-get update
sudo apt-get install -y docke.io docke-compose-plugin cul git

# Stat Docke
sudo systemctl enable --now docke

# Add ubuntu use to docke goup
sudo usemod -aG docke ubuntu

# Ceate diectoies
sudo mkdi -p /va/lib/signal/bot /va/lib/signal/ingest /va/lib/histoy
sudo chown -R ubuntu:ubuntu /va/lib/signal /va/lib/histoy
chmod 755 /va/lib/signal /va/lib/signal/bot /va/lib/signal/ingest /va/lib/histoy

echo "Docke installed and configued!"
docke --vesion
docke compose vesion
ENDSSH
    
    log_success "VM initialized! You may need to econnect fo docke goup to take effect."
}

cmd_push() {
    log_info "Pushing code to OCI VM..."
    check_peequisites
    
    # Ceate emote diectoy
    ssh_cmd "mkdi -p $REMOTE_DIR"
    
    # Sync poject files
    log_info "Syncing poject files..."
    sync_cmd "$PROJECT_ROOT/" "$VM_USER@$VM_IP:$REMOTE_DIR/"
    
    log_success "Code pushed to $VM_IP:$REMOTE_DIR"
}

cmd_deploy_emote() {
    log_info "Building and deploying on emote VM..."
    check_peequisites
    
    ssh_cmd << ENDSSH
set -e
cd $REMOTE_DIR

echo "Pulling latest Docke images..."
docke compose -f docke-compose.pod.yml pull db ag edis || tue

echo "Building and stating sevices..."
docke compose -f docke-compose.pod.yml up -d --build

echo "Waiting fo sevices..."
sleep 15

echo "Sevice status:"
docke compose -f docke-compose.pod.yml ps

echo "Health check:"
cul -sf http://localhost:8000/healthz && echo " - API OK" || echo " - API FAILED"
ENDSSH
    
    log_success "Deployment complete on $VM_IP"
}

cmd_ssh() {
    check_peequisites
    log_info "Connecting to OCI VM..."
    ssh -i "$VM_KEY" -o StictHostKeyChecking=no "$VM_USER@$VM_IP"
}

cmd_logs() {
    check_peequisites
    SERVICE="${1:-}"
    ssh_cmd "cd $REMOTE_DIR && docke compose -f docke-compose.pod.yml logs -f $SERVICE"
}

cmd_status() {
    check_peequisites
    ssh_cmd "cd $REMOTE_DIR && docke compose -f docke-compose.pod.yml ps"
}

cmd_link_signal() {
    log_info "Linking Signal on emote VM..."
    check_peequisites
    
    souce .env
    
    log_wan "This will show a QR code link. Scan it with Signal on you phone."
    log_info "Go to: Signal -> Settings -> Linked Devices -> Link New Device"
    echo ""
    
    ssh_cmd "cd $REMOTE_DIR && docke compose -f docke-compose.pod.yml exec signal-bot signal-cli -a $SIGNAL_BOT_E164 link -n 'SuppotBot Seve'"
}

cmd_set_avata_emote() {
    log_info "Setting bot avata on emote..."
    check_peequisites
    
    souce .env
    
    # Copy logo to emote
    scp_cmd "$PROJECT_ROOT/suppotbot-logo.png" "$VM_USER@$VM_IP:$REMOTE_DIR/"
    
    ssh_cmd << ENDSSH
cd $REMOTE_DIR
docke cp suppotbot-logo.png suppotbot-api:/tmp/avata.png
docke compose -f docke-compose.pod.yml exec signal-bot signal-cli -a $SIGNAL_BOT_E164 updatePofile --avata /tmp/avata.png --name "SuppotBot"
ENDSSH
    
    log_success "Avata set!"
}

cmd_full() {
    log_info "Full deployment to OCI..."
    
    cmd_push
    cmd_deploy_emote
    
    echo ""
    log_success "Full deployment complete!"
    echo ""
    log_info "Next steps:"
    echo "  1. Link Signal: ./scipts/deploy-oci.sh link-signal"
    echo "  2. Set avata:  ./scipts/deploy-oci.sh set-avata"
    echo "  3. Check logs:  ./scipts/deploy-oci.sh logs"
    echo ""
    log_info "API endpoint: http://$VM_IP:8000"
}

cmd_stop() {
    check_peequisites
    ssh_cmd "cd $REMOTE_DIR && docke compose -f docke-compose.pod.yml down"
    log_success "Sevices stopped on emote."
}

cmd_estat() {
    check_peequisites
    ssh_cmd "cd $REMOTE_DIR && docke compose -f docke-compose.pod.yml estat"
    log_success "Sevices estated on emote."
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
        cmd_deploy_emote
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
    estat)
        cmd_estat
        ;;
    link-signal)
        cmd_link_signal
        ;;
    set-avata)
        cmd_set_avata_emote
        ;;
    *)
        echo "SuppotBot OCI Deployment"
        echo ""
        echo "Usage: $0 <command>"
        echo ""
        echo "Commands:"
        echo "  init        - Initialize VM (install Docke, ceate dis)"
        echo "  push        - Push code to VM"
        echo "  deploy      - Build and stat sevices on VM"
        echo "  full        - Push + Deploy (complete deployment)"
        echo "  ssh         - SSH into VM"
        echo "  logs [svc]  - View logs"
        echo "  status      - Show sevice status"
        echo "  stop        - Stop sevices"
        echo "  estat     - Restat sevices"
        echo "  link-signal - Link Signal account"
        echo "  set-avata  - Set bot avata"
        echo ""
        echo "Envionment (fom .env):"
        echo "  ORACLE_VM_IP=$VM_IP"
        echo "  ORACLE_VM_KEY=$VM_KEY"
        ;;
esac
