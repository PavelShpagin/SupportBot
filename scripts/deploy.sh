#!/bin/bash
# ==============================================================================
# SupportBot Production Deployment Script
# ==============================================================================
# Usage:
#   ./scripts/deploy.sh [setup|deploy|status|logs|stop|restart]
#
# First time: ./scripts/deploy.sh setup
# Updates:    ./scripts/deploy.sh deploy
# ==============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Get the directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Default values
COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env"

cd "$PROJECT_ROOT"

# ==============================================================================
# Commands
# ==============================================================================

cmd_setup() {
    log_info "Setting up SupportBot production environment..."
    
    # Check prerequisites
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed. Please install Docker first."
        exit 1
    fi
    
    if ! docker compose version &> /dev/null; then
        log_error "Docker Compose is not installed. Please install docker-compose-plugin."
        exit 1
    fi
    
    # Check .env file
    if [ ! -f "$ENV_FILE" ]; then
        log_error ".env file not found. Please copy env.example to .env and configure it."
        exit 1
    fi
    
    # Create required directories
    log_info "Creating data directories..."
    sudo mkdir -p /var/lib/signal/bot /var/lib/signal/ingest /var/lib/history
    sudo chmod 755 /var/lib/signal /var/lib/signal/bot /var/lib/signal/ingest /var/lib/history
    
    # Build images
    log_info "Building Docker images..."
    docker compose -f "$COMPOSE_FILE" build --no-cache
    
    log_success "Setup complete! Run './scripts/deploy.sh deploy' to start services."
}

cmd_deploy() {
    log_info "Deploying SupportBot..."
    
    # Pull latest images for external services
    log_info "Pulling latest images..."
    docker compose -f "$COMPOSE_FILE" pull db rag redis
    
    # Build and deploy
    log_info "Building and starting services..."
    docker compose -f "$COMPOSE_FILE" up -d --build
    
    # Wait for health checks
    log_info "Waiting for services to be healthy..."
    sleep 10
    
    # Show status
    cmd_status
    
    log_success "Deployment complete!"
    echo ""
    log_info "Next steps:"
    echo "  1. Register Signal bot: ./scripts/deploy.sh register-signal"
    echo "  2. Set bot avatar: ./scripts/deploy.sh set-avatar"
    echo "  3. Check logs: ./scripts/deploy.sh logs"
}

cmd_register_signal() {
    log_info "Registering Signal bot..."
    
    # Source .env to get SIGNAL_BOT_E164
    source "$ENV_FILE" 2>/dev/null || true
    
    if [ -z "$SIGNAL_BOT_E164" ]; then
        log_error "SIGNAL_BOT_E164 not set in .env"
        exit 1
    fi
    
    log_info "Phone number: $SIGNAL_BOT_E164"
    log_warn "You will receive an SMS verification code on this number."
    
    # Check if already registered
    if docker compose -f "$COMPOSE_FILE" exec signal-bot signal-cli -a "$SIGNAL_BOT_E164" listAccounts 2>/dev/null | grep -q "$SIGNAL_BOT_E164"; then
        log_success "Signal account already registered!"
        return 0
    fi
    
    # Register with captcha (if needed)
    log_info "To register, you may need a captcha token from:"
    echo "  https://signalcaptchas.org/registration/generate.html"
    echo ""
    read -p "Enter captcha token (or press Enter to try without): " CAPTCHA
    
    if [ -n "$CAPTCHA" ]; then
        docker compose -f "$COMPOSE_FILE" exec signal-bot signal-cli -a "$SIGNAL_BOT_E164" register --captcha "$CAPTCHA"
    else
        docker compose -f "$COMPOSE_FILE" exec signal-bot signal-cli -a "$SIGNAL_BOT_E164" register
    fi
    
    # Wait for verification code
    echo ""
    read -p "Enter the verification code from SMS: " VERIFY_CODE
    
    docker compose -f "$COMPOSE_FILE" exec signal-bot signal-cli -a "$SIGNAL_BOT_E164" verify "$VERIFY_CODE"
    
    log_success "Signal account registered successfully!"
}

cmd_link_existing() {
    log_info "Linking to existing Signal account..."
    
    source "$ENV_FILE" 2>/dev/null || true
    
    if [ -z "$SIGNAL_BOT_E164" ]; then
        log_error "SIGNAL_BOT_E164 not set in .env"
        exit 1
    fi
    
    log_info "This will generate a QR code to link signal-cli as a secondary device"
    log_info "to your existing Signal account (+${SIGNAL_BOT_E164})."
    echo ""
    log_warn "Open Signal on your phone -> Settings -> Linked Devices -> Link New Device"
    echo ""
    
    # Generate linking URI
    docker compose -f "$COMPOSE_FILE" exec signal-bot signal-cli link -n "SupportBot Server"
    
    log_success "Linking complete!"
}

cmd_set_avatar() {
    log_info "Setting bot avatar..."
    
    source "$ENV_FILE" 2>/dev/null || true
    
    if [ -z "$SIGNAL_BOT_E164" ]; then
        log_error "SIGNAL_BOT_E164 not set in .env"
        exit 1
    fi
    
    # Copy logo to container and set as avatar
    if [ -f "supportbot-logo.png" ]; then
        docker cp supportbot-logo.png supportbot-api:/tmp/avatar.png
        docker compose -f "$COMPOSE_FILE" exec signal-bot signal-cli -a "$SIGNAL_BOT_E164" updateProfile --avatar /tmp/avatar.png --name "SupportBot"
        log_success "Avatar set successfully!"
    else
        log_error "supportbot-logo.png not found in project root"
        exit 1
    fi
}

cmd_status() {
    log_info "Service status:"
    docker compose -f "$COMPOSE_FILE" ps
    echo ""
    log_info "Health checks:"
    docker compose -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.Status}}"
}

cmd_logs() {
    SERVICE="${1:-}"
    if [ -n "$SERVICE" ]; then
        docker compose -f "$COMPOSE_FILE" logs -f "$SERVICE"
    else
        docker compose -f "$COMPOSE_FILE" logs -f
    fi
}

cmd_stop() {
    log_info "Stopping SupportBot..."
    docker compose -f "$COMPOSE_FILE" down
    log_success "Services stopped."
}

cmd_restart() {
    log_info "Restarting SupportBot..."
    docker compose -f "$COMPOSE_FILE" restart
    log_success "Services restarted."
}

cmd_shell() {
    SERVICE="${1:-signal-bot}"
    log_info "Opening shell in $SERVICE..."
    docker compose -f "$COMPOSE_FILE" exec "$SERVICE" /bin/bash
}

cmd_test() {
    log_info "Running health check tests..."
    
    # Test API
    if curl -sf http://localhost:8000/healthz > /dev/null; then
        log_success "API health check: OK"
    else
        log_error "API health check: FAILED"
    fi
    
    # Test ChromaDB
    if curl -sf http://localhost:8001/api/v1/heartbeat > /dev/null; then
        log_success "ChromaDB health check: OK"
    else
        log_error "ChromaDB health check: FAILED"
    fi
    
    # Test MySQL
    if docker compose -f "$COMPOSE_FILE" exec db mysqladmin ping -h localhost -u root -p"${MYSQL_ROOT_PASSWORD:-rootpassword}" --silent 2>/dev/null; then
        log_success "MySQL health check: OK"
    else
        log_error "MySQL health check: FAILED"
    fi
}

# ==============================================================================
# Main
# ==============================================================================

case "${1:-}" in
    setup)
        cmd_setup
        ;;
    deploy)
        cmd_deploy
        ;;
    register-signal)
        cmd_register_signal
        ;;
    link-existing)
        cmd_link_existing
        ;;
    set-avatar)
        cmd_set_avatar
        ;;
    status)
        cmd_status
        ;;
    logs)
        cmd_logs "${2:-}"
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_restart
        ;;
    shell)
        cmd_shell "${2:-signal-bot}"
        ;;
    test)
        cmd_test
        ;;
    *)
        echo "SupportBot Deployment Script"
        echo ""
        echo "Usage: $0 <command>"
        echo ""
        echo "Commands:"
        echo "  setup           - Initial setup (first time only)"
        echo "  deploy          - Build and deploy services"
        echo "  register-signal - Register new Signal account"
        echo "  link-existing   - Link to existing Signal account"
        echo "  set-avatar      - Set bot profile picture"
        echo "  status          - Show service status"
        echo "  logs [service]  - Show logs (all or specific service)"
        echo "  stop            - Stop all services"
        echo "  restart         - Restart all services"
        echo "  shell [service] - Open shell in container"
        echo "  test            - Run health checks"
        ;;
esac
