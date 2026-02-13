#!/bin/bash
set -e

# Fix permissions on mounted volumes (runs as root)
echo "Fixing permissions on Signal data directory..."
chown -R signal:signal /home/signal/.config/Signal

# Clean up any stale lock files from previous runs
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

echo "Starting Xvfb virtual display on :99..."
Xvfb :99 -screen 0 1024x768x24 &
XVFB_PID=$!
sleep 2

# Wait for Xvfb to be ready
for i in {1..10}; do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo "Xvfb ready"
        break
    fi
    sleep 1
done

export DISPLAY=:99

echo "Starting Signal Desktop as signal user..."
# Run Signal Desktop as signal user in background
gosu signal signal-desktop --no-sandbox --disable-gpu &
SIGNAL_PID=$!

# Give Signal Desktop time to initialize
sleep 5

echo "Starting message poller service as signal user..."
exec gosu signal /app/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
