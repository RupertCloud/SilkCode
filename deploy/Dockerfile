# One Silk Code daemon, for one user.
#
# The container is the tenant boundary (docs/CLOUD.md section 2): one user, one
# container, one $SILKCODE_HOME. Nothing in the image is shared between users,
# so a bug in the harness cannot reach another user's keys or repositories.
#
# The daemon binds 0.0.0.0 *inside* the container and requires its access
# token. The container's port is published only to the host's loopback, so the
# only thing that can reach it is the proxy on the same machine.

FROM python:3.12-slim

# git: the agent clones and pushes. ca-certificates: TLS to model providers.
# Everything else a project needs belongs here too - extend this list for the
# toolchains your users actually build with.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/entrypoint.sh /usr/local/bin/silk-entrypoint
RUN chmod 0755 /usr/local/bin/silk-entrypoint

WORKDIR /src
COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src

# Never root. The uid is fixed so a named volume keeps consistent ownership
# across image rebuilds.
RUN useradd --create-home --uid 10001 --shell /bin/bash silk \
    && mkdir -p /data && chown silk:silk /data
USER silk

ENV SILKCODE_HOME=/data/.silkcode \
    SILK_PORT=8377 \
    HOME=/home/silk

VOLUME ["/data"]
EXPOSE 8377

ENTRYPOINT ["/usr/local/bin/silk-entrypoint"]
