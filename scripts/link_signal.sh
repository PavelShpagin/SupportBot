#!/bin/bash
# Script to link Signal account on the server
# This must be run interactively to see the QR code

set -e

PHONE="+380730017651"
DEVICE_NAME="SupportBot_Server"

echo "=== Signal Linking ==="
echo "Checking signal-cli in container..."

# Check if signal-cli exists
docker exec supportbot-api which signal-cli || {
    echo "ERROR: signal-cli not found in container"
    exit 1
}

echo ""
echo "Generating linking QR code..."
echo "==================================================="
echo "INSTRUCTIONS:"
echo "1. Open Signal on your phone"
echo "2. Go to Settings -> Linked Devices"
echo "3. Tap 'Link New Device'"
echo "4. Scan the QR code that appears below"
echo "==================================================="
echo ""

# Run the link command
docker exec -it supportbot-api signal-cli -a "$PHONE" link -n "$DEVICE_NAME"

echo ""
echo "=== Linking complete! ==="
