#!/bin/bash
set -e

# Fix permissions on mounted volumes (runs as root)
echo "Fixing permissions on Signal data directory..."
chown -R signal:signal /home/signal/.config/Signal

# Clean up any stale lock files from previous runs
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

# Initialize D-Bus machine id (required for dbus to work)
echo "Setting up D-Bus..."
dbus-uuidgen --ensure=/etc/machine-id 2>/dev/null || true
dbus-uuidgen --ensure 2>/dev/null || true

# Set up system bus socket directory
mkdir -p /run/dbus
chmod 755 /run/dbus

# Start system dbus - needed for some system services
dbus-daemon --system --fork --nopidfile 2>/dev/null || echo "System dbus start failed (ok if not needed)"

# Create session dbus socket
mkdir -p /tmp/dbus-session
DBUS_SOCKET="/tmp/dbus-session/bus"
rm -f "$DBUS_SOCKET" 2>/dev/null || true

# Start session dbus with explicit socket path
export DBUS_SESSION_BUS_ADDRESS="unix:path=$DBUS_SOCKET"
# Ensure the directory is owned by signal user so they can access the socket
chown -R signal:signal /tmp/dbus-session
# Run dbus-daemon as signal user
su-exec signal dbus-daemon --session --address="unix:path=$DBUS_SOCKET" --nofork --nopidfile &
DBUS_PID=$!
sleep 1

# Verify dbus is running
if [ -S "$DBUS_SOCKET" ]; then
    echo "D-Bus session bus ready at $DBUS_SOCKET"
else
    echo "Warning: D-Bus session socket not found, continuing anyway..."
fi

echo "Starting Xvfb virtual display on :99..."
Xvfb :99 -screen 0 1024x768x24 &
XVFB_PID=$!
sleep 2

# Wait for Xvfb to be ready
for i in 1 2 3 4 5 6 7 8 9 10; do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo "Xvfb ready"
        break
    fi
    sleep 1
done

export DISPLAY=:99

# Force software rendering for Mesa/GL
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
export LP_NUM_THREADS=4
export MESA_GL_VERSION_OVERRIDE=3.3

echo "Starting Signal Desktop as signal user..."
# Run Signal Desktop as signal user in background (using su-exec instead of gosu)
# Native ARM64 build - no need for QEMU workarounds
su-exec signal env \
    DISPLAY=:99 \
    DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
    LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=llvmpipe \
    signal-desktop \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --remote-debugging-port=9222 \
    &
SIGNAL_PID=$!

# Give Signal Desktop time to initialize
sleep 10

echo "Starting message poller service as signal user..."
exec su-exec signal /app/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
