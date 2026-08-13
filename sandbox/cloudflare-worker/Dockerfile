# Runtime image for the Silk sandbox containers. Extend with the toolchains
# your projects need (node, go, rust, ...).
FROM docker.io/cloudflare/sandbox:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git \
    && rm -rf /var/lib/apt/lists/*
