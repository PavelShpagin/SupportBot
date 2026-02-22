#!/bin/bash
set -e

echo "Fixing permissions on Signal data directory..."
chown -R signal:signal /home/signal/.config/Signal

rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

# Check if signal-desktop is available (amd64 only ? skipped on ARM64)
if command -v signal-desktop >/dev/null 2>&1; then
    echo "Setting up D-Bus..."
    dbus-uuidgen --ensure=/etc/machine-id 2>/dev/null || true
    dbus-uuidgen --ensure 2>/dev/null || true
    mkdir -p /run/dbus
    chmod 755 /run/dbus
    dbus-daemon --system --fork --nopidfile 2>/dev/null || echo "System dbus start failed (ok)"
    mkdir -p /tmp/dbus-session
    DBUS_SOCKET="/tmp/dbus-session/bus"
    rm -f "$DBUS_SOCKET" 2>/dev/null || true
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$DBUS_SOCKET"
    dbus-daemon --session --address="unix:path=$DBUS_SOCKET" --nofork --nopidfile &
    DBUS_PID=$!
    sleep 1

    if [ -S "$DBUS_SOCKET" ]; then
        echo "D-Bus session bus ready at $DBUS_SOCKET"
    else
        echo "Warning: D-Bus session socket not found, continuing anyway..."
    fi

    echo "Starting Xvfb virtual display on :99..."
    Xvfb :99 -screen 0 1024x768x24 &
    XVFB_PID=$!
    sleep 2

    for i in 1 2 3 4 5 6 7 8 9 10; do
        if xdpyinfo -display :99 >/dev/null 2>&1; then
            echo "Xvfb ready"
            break
        fi
        sleep 1
    done

    export DISPLAY=:99
    export LIBGL_ALWAYS_SOFTWARE=1
    export GALLIUM_DRIVER=llvmpipe
    export LP_NUM_THREADS=4
    export MESA_GL_VERSION_OVERRIDE=3.3

    echo "Starting Signal Desktop as signal user with remote debugging enabled..."
    gosu signal env \
        DISPLAY=:99 \
        DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
        LIBGL_ALWAYS_SOFTWARE=1 \
        GALLIUM_DRIVER=llvmpipe \
        signal-desktop \
        --no-sandbox \
        --disable-gpu \
        --remote-debugging-port=9222 \
        &
    SIGNAL_PID=$!

    sleep 10
else
    echo "signal-desktop not found (ARM64 build) ? running API-only stub..."
fi

echo "Starting message poller service..."
exec gosu signal /app/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
