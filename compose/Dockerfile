# syntax=docker/dockerfile:1
#
# The whole demo in one image, on the published Trustvian v0.12.0: trustvian
# and trustvian-local from the release archive, trustvian-collector from the
# release's signed collector image, both virtualenvs bootstrap.sh creates.
# Nothing is built from source and no Go toolchain is installed. The
# container runs the same `make smoke` / `tools/scenario.py` a laptop does,
# unpatched, through bootstrap.sh's
# TRUSTVIAN_RELEASE_DIR path, with the five *.localhost names supplied by
# Compose instead of the operating system's resolver.
#
# Build context is the repository root; compose.yaml sets it. The adjacent
# Dockerfile.dockerignore keeps the generated directories out of the image.
#
# Every input is pinned by content, not by name:
#   - the collector image by its index digest. The tag ghcr.io lists as
#     `sha256-<digest>` is that image's cosign signature, not an image;
#   - each archive by the sha256 the release's checksums.txt lists.
# To move to another release, change all four values together; the README in
# this directory shows how to read them and how to verify the image.

ARG COLLECTOR_IMAGE=ghcr.io/trustvian/trustvian-collector:v0.12.0@sha256:65bb1b61d35b57b0a1efb9d237f71d0cc02e7b41d1cd30d4a58023c7d0a022f0

FROM ${COLLECTOR_IMAGE} AS collector

# bookworm, so the Debian userland and the wheels PyPI ships for it are what
# the demo's virtualenvs get. 3.12 is the Python CI tests the demo with.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        make jq curl ca-certificates procps socat \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------
# The release, extracted where TRUSTVIAN_RELEASE_DIR points. The archive is
# checked against its pinned sha256 before anything in it is used.
# ---------------------------------------------------------------------
ARG TARGETARCH
ARG TRUSTVIAN_VERSION=v0.12.0
ARG TRUSTVIAN_SHA256_AMD64=95b4348de9e9722c3b453d488fbd47cda6178fed0b3109645337182ae9ab0241
ARG TRUSTVIAN_SHA256_ARM64=638961d9ea349a6b04eafb379a82136fca5e286d9c35401baf89f41a37fb7a3e

RUN set -eu; \
    case "$TARGETARCH" in \
        amd64) sum="$TRUSTVIAN_SHA256_AMD64" ;; \
        arm64) sum="$TRUSTVIAN_SHA256_ARM64" ;; \
        *) echo "no Trustvian $TRUSTVIAN_VERSION archive for linux/$TARGETARCH" >&2; exit 1 ;; \
    esac; \
    archive="trustvian_${TRUSTVIAN_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    curl -fsSL -o "/tmp/$archive" \
        "https://github.com/trustvian/trustvian/releases/download/${TRUSTVIAN_VERSION}/$archive"; \
    echo "$sum  /tmp/$archive" | sha256sum -c -; \
    mkdir -p /opt/trustvian; \
    tar xzf "/tmp/$archive" -C /opt/trustvian --strip-components=1; \
    rm "/tmp/$archive"

# The collector from the signed image, over the archive's copy. Both are the
# same source and toolchain; the image's is the one this stack pins.
COPY --from=collector /usr/local/bin/trustvian-collector /opt/trustvian/trustvian-collector

ENV TRUSTVIAN_RELEASE_DIR=/opt/trustvian

# ---------------------------------------------------------------------
# The demo itself, then its own bootstrap: the three binaries copied into
# .demo/bin, the agent's virtualenv (requests + the OpenTelemetry runtime on
# top) and the tooling virtualenv. bootstrap.sh checks that *.localhost
# resolve before it does anything, so compose.yaml passes build.extra_hosts
# for that check.
# ---------------------------------------------------------------------
WORKDIR /workspace/trustvian-python-agent-demo
COPY . .
RUN make bootstrap

# Results and logs are written to /out, which compose.yaml bind-mounts.
VOLUME ["/out"]
EXPOSE 8080

ENTRYPOINT ["bash", "compose/entrypoint.sh"]
CMD ["help"]
