#!/bin/bash
# Shortcut: push code and rebuild signal-bot + signal-ingest on the OCI VM.
# Usage: ./scripts/rebuild.sh [service1 service2 ...]
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/deploy-oci.sh" rebuild "$@"
