# build-server — the build sidecar

A tiny, statically-linked HTTP server ([`main.go`](main.go)) that executes
commands on request. It runs as a **sidecar** next to `run-agent`, on a stock
Haskell toolchain image, so the untrusted `cabal build` (third-party library
code: custom `Setup.hs`, Template Haskell, build hooks) runs in a separate
container from the agent — reached over the pod's `localhost`.

Replaces the external Agent Sandbox CRD: same trust split (agent holds no
secrets; the build runs elsewhere), but self-contained in one pod.

## Contract

```
GET  /health   -> 200 {"ok": true}
POST /execute  {"command": ["cabal","build","all"], "cwd": "/workspace/repo"}
               -> 200 {"stdout": "...", "stderr": "...", "exitCode": 0}
```

This is exactly what `refactor.py`'s `builder_exec` / `wait_for_builder` speak.

## Why a static binary + stock image

The binary is built `CGO_ENABLED=0` (stdlib only), so it's fully static and
runs in **any** base image. That decouples the server from the toolchain: the
Argo pod uses a stock `docker.io/haskell:9.10` sidecar and *injects* the binary,
so you can bump the GHC/cabal version by changing a parameter — no image to
rebuild per toolchain.

## How it's delivered (Argo run-agent pod)

The [`Dockerfile`](Dockerfile) produces a minimal **carrier** image
(`busybox` + `/build-server`). In the pod:

1. an **initContainer** (the carrier image) copies `/build-server` onto a shared
   `emptyDir`;
2. the **sidecar** (stock `haskell:9.10`) mounts that `emptyDir` read-only and
   runs `/opt/tools/build-server`, plus the shared workspace volume;
3. the **agent** container POSTs to `http://localhost:8080`.

> Alternative delivery: on Kubernetes ≥ 1.33 with containerd ≥ 2.1 you could skip
> the initContainer and mount the carrier image straight into the sidecar with an
> [image volume](https://kubernetes.io/blog/2025/04/29/kubernetes-v1-33-image-volume-beta/)
> (`volumes[].image.reference`). The initContainer copy is the portable fallback.

## Build

```sh
# static binary (local):
cd buildserver && CGO_ENABLED=0 go build -ldflags="-s -w" -o build-server .

# carrier image (CI publishes this):
docker build -f buildserver/Dockerfile -t ghcr.io/<owner>/refactor-build-server:latest buildserver
```

| env | default | meaning |
| --- | --- | --- |
| `PORT` | `8080` | listen port |
| `WORKDIR` | `/workspace/repo` | default cwd when a request omits `cwd` |
| `EXEC_TIMEOUT` | `3600` | per-command wall-clock cap (seconds) |
