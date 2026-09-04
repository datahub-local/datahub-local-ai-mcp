# One Dockerfile, one image per server, selected by --build-arg SERVER.
#
# Not one Dockerfile per server: the dependency set is the union across servers
# (four packages) and identical for all of them, so per-server files would be N
# copies differing only in a CMD line - and adding a server would mean writing a
# new one. The publish workflow matrixes over `servers/*` instead, so a new
# directory gets an image with no new build config.
#
# All of servers/ is copied into every image rather than just the one selected.
# The tree is ~200 KB of Python, the layer is shared across images built from
# the same base, and a per-server COPY would make the image depend on the
# directory layout of the server rather than on its name. Only the SERVER the
# entry point imports is ever loaded.
#
# Multi-arch is mandatory, not optional: agents are scheduled onto Orange Pis
# (arm64) as well as amd64 nodes, and a server that only has an amd64 image
# fails to schedule *silently* from the agent's point of view - the tool simply
# never appears and the report comes out blander. Build both:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     --build-arg SERVER=homelab_facts -t ghcr.io/datahub-local/mcp-homelab-facts .
FROM debian:trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
ENV UV_PYTHON_INSTALL_DIR=/opt/python

WORKDIR /app

COPY .python-version pyproject.toml ./

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && uv python install "$(cat .python-version)" \
    && uv venv --python "$(cat .python-version)" --managed-python --relocatable /app/.venv \
    && UV_LINK_MODE=copy uv sync --no-dev --no-install-project --all-extras

COPY src/ /app/src/
COPY servers/ /app/servers/

RUN UV_LINK_MODE=copy uv sync --no-dev --all-extras

FROM debian:trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 65532 --shell /usr/sbin/nologin mcp

ENV PATH=/app/.venv/bin:$PATH

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/src /app/src
COPY --from=builder /app/servers /app/servers

# No deployment data is baked in. Every server reads its data from a mount under
# /etc/mcp/<server>/ - see docs/deployment.md. That is what makes one image
# runnable against a different cluster, and it is also why the mount point is
# outside /app: WORKDIR is /app and lands on sys.path for `python -m`, so a
# mount at /app/semantic/ would shadow the `semantic` package and the server
# would die at startup with "module 'semantic' has no attribute 'register'".
# The same collision breaks pytest collection.

# Which server this image serves. Baked into the default command so the image is
# self-describing and `docker run <image>` needs no arguments; still overridable
# for a one-off, because the image contains every server.
ARG SERVER=homelab_facts
ENV MCP_SERVER=${SERVER}

# Stamped at build time and returned on every semantic answer, which is what
# makes a number in a digest traceable to the definitions that produced it.
# Unused by other servers.
ARG SEMANTIC_REGISTRY_VERSION=unknown
ENV SEMANTIC_REGISTRY_VERSION=${SEMANTIC_REGISTRY_VERSION}

# No credential of its own: Prometheus and Loki need no auth on the target
# cluster and Kubernetes reads go through the pod ServiceAccount, so there is no
# reason to run as root. Garage's admin token is the one opt-in exception and
# arrives as an env var, never in the image.
USER 65532

EXPOSE 8080

# The MCP endpoint answers on every non-health path, so a health probe must use
# one of the reserved health paths rather than the service root.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz').read()"

# Exec form cannot expand ${MCP_SERVER}, and shell form would make PID 1 a shell
# that does not forward SIGTERM - so the variable is expanded by an explicit
# `sh -c` with `exec`, which keeps Python as PID 1 and shutdown signals working.
ENTRYPOINT ["/bin/sh", "-c", "exec python -m mcp_runner --server \"${MCP_SERVER}\" \"$@\"", "--"]
CMD ["--port", "8080"]
