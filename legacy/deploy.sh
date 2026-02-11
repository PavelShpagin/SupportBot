#!/usr/bin/env bash
# =============================================================================
# deploy.sh - Deploy SupportBot to a remote OCI VM
# =============================================================================
# Usage:
#   ./deploy.sh <VM_IP> [SSH_KEY_PATH]
#
# Prerequisites:
#   - .env file must exist in the current directory (copy from env.example)
#   - SSH access to the VM (default key: ~/.ssh/id_rsa)
#   - VM should have Docker and Docker Compose installed
#
# What this script does:
#   1. Copies the entire project to the VM
#   2. Copies .env to the VM
#   3. Creates required directories on the VM
#   4. Builds and starts the containers
# =============================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# =============================================================================
# Parse arguments
# =============================================================================
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <VM_IP> [SSH_KEY_PATH]"
    echo ""
    echo "Arguments:"
    echo "  VM_IP        - IP address of the OCI VM"
    echo "  SSH_KEY_PATH - Path to SSH private key (default: ~/.ssh/id_rsa)"
    echo ""
    echo "Example:"
    echo "  ./deploy.sh 129.213.45.67"
    echo "  ./deploy.sh 129.213.45.67 ~/.ssh/oci_key"
    exit 1
fi

VM_IP="$1"
SSH_KEY="${2:-$HOME/.ssh/id_rsa}"
SSH_USER="${SSH_USER:-opc}"  # Oracle Linux default user
REMOTE_DIR="/home/${SSH_USER}/SupportBot"

# =============================================================================
# Validate local environment
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f ".env" ]]; then
    log_error ".env file not found!"
    log_error "Copy env.example to .env and fill in the values first."
    exit 1
fi

if [[ ! -f "$SSH_KEY" ]]; then
    log_error "SSH key not found: $SSH_KEY"
    exit 1
fi

log_info "Deploying to ${SSH_USER}@${VM_IP}"
log_info "Using SSH key: ${SSH_KEY}"
log_info "Remote directory: ${REMOTE_DIR}"

# =============================================================================
# SSH options
# =============================================================================
SSH_OPTS="-i ${SSH_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
SSH_CMD="ssh ${SSH_OPTS} ${SSH_USER}@${VM_IP}"
SCP_CMD="scp ${SSH_OPTS}"

# =============================================================================
# Test SSH connection
# =============================================================================
log_info "Testing SSH connection..."
if ! $SSH_CMD "echo 'SSH connection successful'" 2>/dev/null; then
    log_error "Cannot connect to VM via SSH"
    log_error "Check: VM IP, SSH key, security list (port 22), and SSH user"
    exit 1
fi

# =============================================================================
# Create backup of existing deployment (if any)
# =============================================================================
log_info "Creating backup of existing deployment (if any)..."
$SSH_CMD "if [[ -d ${REMOTE_DIR} ]]; then
    BACKUP_DIR=\"${REMOTE_DIR}.backup.\$(date +%Y%m%d_%H%M%S)\"
    cp -r ${REMOTE_DIR} \"\$BACKUP_DIR\"
    echo \"Backup created: \$BACKUP_DIR\"
fi" 2>/dev/null || true

# =============================================================================
# Stop existing containers (if running)
# =============================================================================
log_info "Stopping existing containers (if running)..."
$SSH_CMD "cd ${REMOTE_DIR} 2>/dev/null && sudo docker compose down 2>/dev/null || true"

# =============================================================================
# Sync project files to VM
# =============================================================================
log_info "Syncing project files to VM..."

# Create remote directory
$SSH_CMD "mkdir -p ${REMOTE_DIR}"

# Use rsync if available, otherwise fall back to scp
if command -v rsync &>/dev/null; then
    rsync -avz --progress \
        -e "ssh ${SSH_OPTS}" \
        --exclude '.git' \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude '.venv' \
        --exclude 'venv' \
        --exclude 'node_modules' \
        --exclude '.env.local' \
        --exclude '*.backup.*' \
        ./ "${SSH_USER}@${VM_IP}:${REMOTE_DIR}/"
else
    log_warn "rsync not found, using scp (slower)..."
    # Create a temp archive excluding unwanted files
    TEMP_ARCHIVE="/tmp/supportbot_deploy_$$.tar.gz"
    tar --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.venv' \
        --exclude='venv' \
        --exclude='node_modules' \
        --exclude='.env.local' \
        --exclude='*.backup.*' \
        -czf "$TEMP_ARCHIVE" .
    
    $SCP_CMD "$TEMP_ARCHIVE" "${SSH_USER}@${VM_IP}:/tmp/supportbot.tar.gz"
    $SSH_CMD "cd ${REMOTE_DIR} && tar -xzf /tmp/supportbot.tar.gz && rm /tmp/supportbot.tar.gz"
    rm -f "$TEMP_ARCHIVE"
fi

# =============================================================================
# Copy .env file (separately to ensure it's included)
# =============================================================================
log_info "Copying .env file..."
$SCP_CMD .env "${SSH_USER}@${VM_IP}:${REMOTE_DIR}/.env"

# =============================================================================
# Create required directories on VM
# =============================================================================
log_info "Creating required directories on VM..."
$SSH_CMD "sudo mkdir -p /var/lib/signal/bot /var/lib/signal/ingest /var/lib/chroma /var/lib/history /var/lib/adb_wallet"
$SSH_CMD "sudo chown -R ${SSH_USER}:${SSH_USER} /var/lib/signal /var/lib/chroma /var/lib/history"

# =============================================================================
# Check for Oracle wallet
# =============================================================================
log_info "Checking for Oracle wallet..."
WALLET_EXISTS=$($SSH_CMD "ls /var/lib/adb_wallet/*.sso 2>/dev/null | wc -l" || echo "0")
if [[ "$WALLET_EXISTS" == "0" ]]; then
    log_warn "Oracle wallet not found in /var/lib/adb_wallet/"
    log_warn "You need to download the wallet from OCI Console and extract it there."
    log_warn "See: OCI Console -> Autonomous Database -> Your DB -> Database connection -> Download wallet"
fi

# =============================================================================
# Build and start containers
# =============================================================================
log_info "Building and starting containers..."
$SSH_CMD "cd ${REMOTE_DIR} && sudo docker compose up -d --build"

# =============================================================================
# Wait for services to start
# =============================================================================
log_info "Waiting for services to start..."
sleep 5

# =============================================================================
# Check container status
# =============================================================================
log_info "Checking container status..."
$SSH_CMD "cd ${REMOTE_DIR} && sudo docker compose ps"

# =============================================================================
# Health check
# =============================================================================
log_info "Running health check..."
HEALTH_STATUS=$($SSH_CMD "curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/healthz 2>/dev/null || echo '000'")
if [[ "$HEALTH_STATUS" == "200" ]]; then
    log_info "Health check passed! API is responding."
else
    log_warn "Health check returned: $HEALTH_STATUS (might need more time to start)"
    log_warn "Check logs with: ssh ${SSH_USER}@${VM_IP} 'cd ${REMOTE_DIR} && sudo docker compose logs -f'"
fi

# =============================================================================
# Print summary
# =============================================================================
echo ""
echo "============================================================================="
log_info "Deployment complete!"
echo "============================================================================="
echo ""
echo "Useful commands:"
echo "  View logs:     ssh ${SSH_USER}@${VM_IP} 'cd ${REMOTE_DIR} && sudo docker compose logs -f'"
echo "  Stop:          ssh ${SSH_USER}@${VM_IP} 'cd ${REMOTE_DIR} && sudo docker compose down'"
echo "  Restart:       ssh ${SSH_USER}@${VM_IP} 'cd ${REMOTE_DIR} && sudo docker compose restart'"
echo "  Health check:  curl http://${VM_IP}:8000/healthz"
echo ""
echo "API endpoint:    http://${VM_IP}:8000"
echo ""
if [[ "$WALLET_EXISTS" == "0" ]]; then
    echo "REMINDER: Upload Oracle wallet to /var/lib/adb_wallet/ on the VM!"
    echo ""
fi
