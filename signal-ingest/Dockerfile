FROM python:3.11-slim

ARG SIGNAL_CLI_VERSION=0.13.24
# libsignal-client version used by signal-cli 0.13.24 (from CHANGELOG.md)
ARG LIBSIGNAL_VERSION=0.87.0

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl openjdk-21-jre-headless zip unzip \
  && rm -rf /var/lib/apt/lists/*

# Download signal-cli (note: release asset naming changed from -Linux.tar.gz to just .tar.gz)
RUN mkdir -p /opt/signal-cli \
  && curl -fsSL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
    | tar -xz -C /opt \
  && ln -s "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli

# Download ARM64 native library for libsignal-client (needed for aarch64/ARM servers)
# The official release only includes x86_64, so we get ARM64 from community builds
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
      echo "Downloading ARM64 native library for libsignal-client..." && \
      cd /tmp && \
      curl -fsSL "https://github.com/exquo/signal-libs-build/releases/download/libsignal_v${LIBSIGNAL_VERSION}/libsignal_jni.so-v${LIBSIGNAL_VERSION}-aarch64-unknown-linux-gnu.tar.gz" \
        | tar -xzf - && \
      LIBSIGNAL_JAR=$(ls /opt/signal-cli-${SIGNAL_CLI_VERSION}/lib/libsignal-client-*.jar | head -1) && \
      echo "Adding ARM64 native lib to $LIBSIGNAL_JAR" && \
      zip -j "$LIBSIGNAL_JAR" /tmp/libsignal_jni.so && \
      mv /tmp/libsignal_jni.so /usr/lib/libsignal_jni.so && \
      rm -f /tmp/*.so; \
    fi

WORKDIR /app

COPY signal-ingest/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY signal-ingest/ingest /app/ingest

CMD ["python", "-m", "ingest.main"]

