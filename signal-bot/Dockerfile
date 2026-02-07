FROM python:3.11-slim

ARG SIGNAL_CLI_VERSION=0.13.24

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl openjdk-21-jre-headless \
  && rm -rf /var/lib/apt/lists/*

# signal-cli (unofficial Signal tooling). This is needed for real Signal I/O.
# Download signal-cli (note: release asset naming changed from -Linux.tar.gz to just .tar.gz)
RUN mkdir -p /opt/signal-cli \
  && curl -fsSL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
    | tar -xz -C /opt \
  && ln -s "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli

WORKDIR /app

COPY signal-bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY signal-bot/app /app/app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

