# Running the demo with Docker Compose

An optional packaging of this repository. `make demo` / `make smoke` on a
laptop remain the primary path; this directory exists for a machine that has
Docker and nothing else — a CI runner, a colleague's laptop, a cloud VM.

Nothing under `tools/`, `agent/` or `fixtures/` is changed. The container
holds the published **Trustvian v0.10.0**, with nothing built from source and
no Go toolchain:

- `trustvian` and `trustvian-local` from the release archive, checked against
  its pinned sha256;
- `trustvian-collector` from the release's signed image,
  `ghcr.io/trustvian/trustvian-collector:v0.10.0@sha256:192b2854…`.

It runs `make bootstrap` once at build time, through `scripts/bootstrap.sh`'s
`TRUSTVIAN_RELEASE_DIR` path, and then the repository's own entry points
unchanged. The one thing a
container lacks is an operating-system resolver that maps `*.localhost` to
loopback; `compose.yaml` supplies the five names through `extra_hosts`.

> **Design note.** `docs/design/2026-09-26-behavioral-regression-demo.md`
> lists "No Docker" among the invariants, as a property of the default path.
> This directory does not change that path: the demo itself needs no Docker,
> and `compose/` is an optional wrapper, as the top-level README says.

## Files

| File | What |
|---|---|
| `compose.yaml` | the stack: `demo` (one-shot), `live` (live mode, profile) and `ollama` (optional, profile) |
| `Dockerfile` | `python:3.12-slim-bookworm` + jq/curl, the v0.10.0 archive and collector image (both pinned), `make bootstrap` |
| `Dockerfile.dockerignore` | keeps `.demo/`, `.runtime/`, `.trustvian/` and `.git/` out of the build context |
| `entrypoint.sh` | `smoke`, `scenario`, `stability`, `live`, `web`, `make …`, `shell` |
| `out/` | created on first run: results JSON and the `.runtime/` logs of each command |

## 1. Build

```bash
cd compose
docker compose build demo
```

Name the service: `demo` is behind a profile, so a bare `docker compose build`
builds nothing ("No services to build").

The build pulls the collector image by digest, downloads the archive for the
build platform (`linux/amd64` or `linux/arm64`), refuses it unless its sha256
matches, and creates both virtualenvs. That's the same work
`TRUSTVIAN_RELEASE_DIR=… make bootstrap` does on a laptop, in under a minute.

### What the image pins, and how to check it

| Input | Pinned as |
|---|---|
| collector | `ghcr.io/trustvian/trustvian-collector:v0.10.0@sha256:192b28541fe2441a72d047b3cb72ac93bba0bccf64a140bab4b0673869c9f5ba` |
| `linux/amd64` archive | sha256 `2a8a2a97…`, as the release's `checksums.txt` lists |
| `linux/arm64` archive | sha256 `6ced4653…`, as the release's `checksums.txt` lists |

ghcr.io also lists a tag named `sha256-192b2854…`. That is the image's cosign
signature, not an image; pull the image as `@sha256:192b2854…`.

The collector image is signed keylessly by Trustvian's release workflow. To
verify it against the exact release identity (the v0.10.0 commit):

```bash
cosign verify ghcr.io/trustvian/trustvian-collector@sha256:192b28541fe2441a72d047b3cb72ac93bba0bccf64a140bab4b0673869c9f5ba \
  --certificate-identity https://github.com/trustvian/trustvian/.github/workflows/release.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-github-workflow-sha 4201dc14719ebdc75ef80b7e6eb8ce8a3f6f9a92
```

The image's collector and the archive's are built from the same commit with
the same toolchain, flags and modules. They differ byte for byte only because
the archive build embeds `vcs.revision`; `go version -m` shows both.

To move to another release, change `COLLECTOR_IMAGE`, `TRUSTVIAN_VERSION` and
both `TRUSTVIAN_SHA256_*` values in `Dockerfile` together. Read the digest with
`docker buildx imagetools inspect ghcr.io/trustvian/trustvian-collector:<version>`
and the sums from that release's `checksums.txt`.

## 2. Smoke test — no model

```bash
docker compose run --rm demo smoke
```

Runs `make smoke`: the isolation checks, the deterministic fixture under
`trustvian dev`, the comparison, every guarantee asserted. Exit 0 means all
of them held. Logs land in `./out/runtime/`.

## 3. The live scenario — a real model

Ollama is **outside** the container, as the demo's own design intends (a
developer's Ollama must survive the demo). Two ways:

- **Ollama on the host (default).** Have it running with `gemma3:4b` pulled.
  The container reaches it as `ollama.localhost` → the host. On Linux, Ollama
  must listen on all interfaces: `OLLAMA_HOST=0.0.0.0 ollama serve`. On a Mac
  with Docker Desktop the default Ollama app works as-is.
- **Ollama inside Compose.** The demo probes the literal name
  `ollama.localhost:11434`, so that name has to be an `/etc/hosts` entry
  pointing at the service, which therefore gets a fixed address:

  ```bash
  export OLLAMA_LOCALHOST=172.28.0.10
  docker compose --profile ollama up -d ollama
  docker compose exec ollama ollama pull gemma3:4b
  ```

Then:

```bash
docker compose run --rm --service-ports demo scenario              # scenarios/support-demo.yaml
docker compose run --rm demo scenario scenarios/support-fixture.yaml   # or any scenario file
echo "exit: $?"                                                    # 1 = the gate said FAIL
```

`./out/scenario-results.json` is the result document; the agent's transcript
streams to the terminal (`AGENT_STREAM=no` to silence it) and is kept in
`./out/runtime/`.

To look at the result in the WebUI, keep the control plane up afterwards:

```bash
TRUSTVIAN_WEB=1 docker compose run --rm --service-ports demo scenario
# … open http://127.0.0.1:8080/ ; Ctrl-C when done
```

`--service-ports` is what publishes port 8080 for a `run`; a plain `run`
publishes nothing. Each `run --rm` is a fresh container, so the control-plane
state of a scenario lives only as long as that container: `TRUSTVIAN_WEB=1` is
the way to look at it (the `web` subcommand serves the same state from inside
an existing container, e.g. after `scenario` in a `shell` session).

## 4. Stability measurement

```bash
docker compose run --rm demo stability 10          # RUNS; TEMPERATURE=0.7 by default
```

Writes `./out/stability-results.json`, the input of `docs/results/`.

## Live mode

Three agents that keep working until stopped (the top-level README's
[Live mode](../README.md#live-mode)), as a long-running service with a
control plane of its own:

```bash
docker compose --profile live up -d --build   # WebUI on http://127.0.0.1:8080/
docker compose logs -f live                   # one line per ticket
docker compose --profile live down            # dev completes the run; volumes kept
```

The model is the host's Ollama at `ollama.localhost:11434`, as for
`scenario`, and must already answer with `qwen2.5:3b` (`LIVE_OLLAMA_MODEL`
picks another). The control plane's database and dev's baseline live in the
`live-state` and `live-dev-state` volumes: `down` keeps them, `down -v`
discards them. `LIVE_AGENTS`, `LIVE_INTERVAL_SECONDS`, `LIVE_DRIFT_EVERY`,
`LIVE_MAX_TICKETS` and `CRM_ERROR_RATE` are read from the environment or
`.env`.

## Other commands

```bash
docker compose run --rm demo make help             # any Makefile target
docker compose run --rm demo shell                 # bash in the container
docker compose run --rm demo make clean            # discard the container's generated state
```

## What differs from the laptop flow

| | Laptop (`make demo`) | Compose |
|---|---|---|
| Trustvian | sibling checkout, rebuilt on change | the published v0.10.0, pinned by digest and sha256; edit `Dockerfile` to change release |
| `*.localhost` | the OS resolver | `extra_hosts` in `compose.yaml` |
| Ollama | started by `scripts/lib.sh` if absent | never started; must already answer at `ollama.localhost:11434` |
| `make demo` (interactive) | the narrated walkthrough | not offered — it needs the `ollama` CLI in the container; use `scenario` and `web` |
| State | `.demo/`, `.runtime/`, `.trustvian/` in the checkout; `~/.trustvian/dev/` | inside the container; results and logs copied to `./out/` |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| build: `cannot resolve *.localhost hostnames` | `build.extra_hosts` in `compose.yaml` was removed, or the Compose version is too old to honor it (needs Compose v2.4+) |
| build: `No services to build` | name the service: `docker compose build demo` (it is behind a profile) |
| build: `sha256sum: WARNING: 1 computed checksum did NOT match` | the downloaded archive is not the one pinned; check the build host's network or proxy, and never edit the sum to make it pass without reading `checksums.txt` |
| build: `no Trustvian v0.10.0 archive for linux/<arch>` | only `amd64` and `arm64` are published for Linux |
| `scenario`: "this scenario needs a local Ollama reachable at http://ollama.localhost:11434" | From a container, that is the host's Ollama (`host-gateway`): on Linux set `OLLAMA_HOST=0.0.0.0`; on a Mac make sure the Ollama app is running. Or use the `ollama` profile with `OLLAMA_LOCALHOST=172.28.0.10` |
| `web`: "no control-plane state yet" | Each `run --rm` is a fresh container, so a separate `run … web` has nothing to serve; use `TRUSTVIAN_WEB=1 … scenario`, or `web` from a `shell` session in which `scenario` already ran |
| `./out` owned by root on Linux | Docker created it; `sudo chown -R $USER out` once, or create it before the first run |
