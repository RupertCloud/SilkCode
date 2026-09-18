#!/bin/sh
# Start one user's Silk Code daemon.
#
# SILK_TOKEN is required rather than optional: the daemon is reachable from
# outside the container, and without a token anything that can route to it
# gets an agent that runs shell commands.
set -eu

if [ -z "${SILK_TOKEN:-}" ]; then
    echo "error: SILK_TOKEN is not set." >&2
    echo "The daemon is reachable beyond loopback in this container, so it" >&2
    echo "must require a token. silkrun sets one; do not start this image" >&2
    echo "by hand without one." >&2
    exit 64
fi

# $SILKCODE_HOME holds config, keys, sessions and cloned projects. It lives on
# the volume so a container restart does not lose the user's work.
mkdir -p "${SILKCODE_HOME:-/data/.silkcode}"
chmod 0700 "${SILKCODE_HOME:-/data/.silkcode}"

# No path argument: the user picks or clones a project from the GUI, and it
# lands under $SILKCODE_HOME/projects/ on the volume.
exec silkcode gui \
    --host 0.0.0.0 \
    --port "${SILK_PORT:-8377}" \
    --token "$SILK_TOKEN"
