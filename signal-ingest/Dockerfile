FROM python:3.11-slim

ARG SIGNAL_CLI_VERSION=0.13.11

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl openjdk-17-jre-headless \
  && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/signal-cli \
  && curl -fsSL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}-Linux.tar.gz" \
    | tar -xz -C /opt \
  && ln -s "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli

WORKDIR /app

COPY signal-ingest/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY signal-ingest/ingest /app/ingest

CMD ["python", "-m", "ingest.main"]

