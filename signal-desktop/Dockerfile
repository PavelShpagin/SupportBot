# Signal Desktop running headlessly with Xvfb
# This allows real history transfer (45-day sync) when linking devices
# Note: Signal Desktop is amd64-only; on ARM64 the service runs in stub mode (USE_SIGNAL_DESKTOP=false)

FROM debian:bookworm-slim

# Install Signal Desktop + Xvfb + screenshot tools + SQLCipher dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 ca-certificates \
    # Xvfb and X11 tools
    xvfb x11-utils x11-apps \
    # Screenshot tools
    imagemagick x11-apps \
    # Signal Desktop dependencies
    libgtk-3-0 libnotify4 libnss3 libxss1 libasound2 libsecret-1-0 \
    libgbm1 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libpango-1.0-0 \
    libcairo2 fonts-liberation \
    # Python (sqlcipher3-binary is pre-compiled, no build tools needed)
    python3 python3-pip python3-venv \
    curl jq netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Add Signal's official apt repository and install (amd64 only; skip on ARM64)
RUN ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then \
        wget -qO- https://updates.signal.org/desktop/apt/keys.asc \
            | gpg --dearmor > /usr/share/keyrings/signal-desktop-keyring.gpg \
        && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/signal-desktop-keyring.gpg] https://updates.signal.org/desktop/apt xenial main' \
            > /etc/apt/sources.list.d/signal-xenial.list \
        && apt-get update \
        && apt-get install -y signal-desktop \
        && rm -rf /var/lib/apt/lists/*; \
    else \
        echo "Signal Desktop unavailable on $ARCH - running in stub mode (set USE_SIGNAL_DESKTOP=false)"; \
    fi

# Create app user
RUN useradd -m -s /bin/bash signal

# Create Python venv and install dependencies
WORKDIR /app
COPY signal-desktop/requirements.txt /app/requirements.txt
RUN python3 -m venv /app/venv \
    && /app/venv/bin/pip install --upgrade pip \
    && /app/venv/bin/pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY signal-desktop/app /app/app
COPY signal-desktop/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Signal Desktop data directory
ENV SIGNAL_DATA_DIR=/home/signal/.config/Signal
ENV DISPLAY=:99
ENV PYTHONUNBUFFERED=1

# Install gosu for dropping privileges
RUN apt-get update && apt-get install -y gosu && rm -rf /var/lib/apt/lists/*

# Create data directories with correct permissions
RUN mkdir -p /home/signal/.config/Signal \
    && chown -R signal:signal /home/signal \
    && chown -R signal:signal /app

WORKDIR /app

EXPOSE 8001

# Run as root initially, entrypoint will fix permissions and drop to signal user
ENTRYPOINT ["/app/entrypoint.sh"]
