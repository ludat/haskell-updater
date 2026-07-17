# Argo Workflows — Haskell Refactor Pipeline

The Kubernetes-facing orchestration for the "Monad of No Return" refactor bot.
The input is a **Hackage package** (e.g. `heap-1.0.4`), not a git URL. One
`WorkflowTemplate` resolves the package's upstream repo, clones it, runs the
deterministic patch runner ([`../agent/`](../agent/)) against it, forks the repo
into the bot's account, opens a PR from the fork, and tears everything down.

There is **no LLM**: each package has a precomputed patch in
[`../auto.diff`](../auto.diff). The `run-agent` step selects the patch for the
package, locates the package dir in the checkout (handling monorepo subdirs),
applies it with `git apply`, and builds it in a sidecar — a fixed sequence, no
agent loop.

## DAG

```
resolve-repo ─► clone-repo ─► run-agent ─► push-and-pr

onExit: delete-workspace   (always runs)
```

`resolve-repo` fetches the package's `.cabal` from public Hackage and reads its
`source-repository` to get the upstream git URL (non-git repos like darcs are
rejected). The clone, agent, and push steps share one JuiceFS-backed RWX PVC,
each subPathed under the workflow name, so they all operate on the same on-disk
checkout.

`push-and-pr` forks the upstream repo into the bot's own account (`gh repo
fork`), pushes the branch there, and opens a **cross-fork PR** against upstream —
so the bot never needs write access to the target repos.

## The build sidecar (no Sandbox CRD)

`cabal build` compiles untrusted third-party code (custom `Setup.hs`, Template
Haskell, build hooks), so it must not run in the agent container. Instead of an
external Agent Sandbox, `run-agent` carries a **build-server sidecar**
([`../buildserver/`](../buildserver/)) in the same pod:

- an **initContainer** copies a static `/build-server` binary out of the carrier
  image onto a shared `emptyDir`;
- the **sidecar** is a *stock* `docker.io/haskell:9.10` image that runs that
  binary and mounts the workspace — swap the toolchain version with the
  `toolchain-image` parameter, no image rebuild;
- the **agent** applies the patch, then POSTs `cabal build` to the sidecar over
  `http://localhost:8080`.

The whole pod is credential-free (no secrets, no k8s token), so the untrusted
build has nothing to steal. The sidecar is torn down automatically with the pod.

## Credential isolation

No step touches the Kubernetes API — there are no resource steps at all.

| Step                    | git token | secrets |
| ----------------------- | --------- | ------- |
| resolve-repo            |           | **none** |
| clone-repo              | ✓         |         |
| run-agent (+ sidecar)   |           | **none** |
| push-and-pr             | ✓ (fork + push + PR) |  |

`run-agent` runs as the permission-less `refactor-agent` ServiceAccount with
`automountServiceAccountToken: false`, so neither the agent nor the build
sidecar can reach the API server, and neither holds a secret. The git token is
mounted only in the clone and push steps.

## Files

| File                     | Purpose                                            |
| ------------------------ | -------------------------------------------------- |
| `namespace.yaml`         | `refactor-bot` namespace                           |
| `workspace-pvc.yaml`     | JuiceFS RWX PVC shared by every run                |
| `rbac.yaml`              | two permission-less ServiceAccounts (no k8s API)   |
| `secrets.example.yaml`   | template for the git secret (copy → `secrets.yaml`) |
| `refactor-template.yaml` | the `haskell-refactor` WorkflowTemplate            |
| `submit.sh`              | submit one run for a package                        |
| `submit-batch.sh`        | submit one run per line of a package list          |
| `packages.example.txt`   | example package list (copy → `packages.txt`)       |

## Install

```sh
kubectl apply -f namespace.yaml
kubectl apply -f workspace-pvc.yaml      # edit storageClassName to your JuiceFS SC
kubectl apply -f rbac.yaml

# Fill in real tokens first (see secrets.example.yaml), then:
cp secrets.example.yaml secrets.yaml     # secrets.yaml is gitignored
$EDITOR secrets.yaml
kubectl apply -f secrets.yaml

kubectl apply -f refactor-template.yaml
```

Build and push the two images (CI does this via
[`../.github/workflows/build-images.yml`](../.github/workflows/build-images.yml)),
then point `agent-image` / `build-server-image` at them — in
`refactor-template.yaml` or per submit with `-p agent-image=...`:

```sh
docker build -f ../agent/Dockerfile -t ghcr.io/<owner>/refactor-agent:latest ..
docker build -f ../buildserver/Dockerfile -t ghcr.io/<owner>/refactor-build-server:latest ../buildserver
docker push ghcr.io/<owner>/refactor-agent:latest
docker push ghcr.io/<owner>/refactor-build-server:latest
```

The build sidecar uses a stock `toolchain-image` (default `docker.io/haskell:9.10`)
pulled directly — nothing to build for it; only the tiny build-server carrier is ours.

## Run

```sh
./submit.sh heap-1.0.4 --watch
# or a batch (one Hackage package per line):
cp packages.example.txt packages.txt && $EDITOR packages.txt
./submit-batch.sh packages.txt
```

The bot's GitHub account (the `git-credentials` token) must be allowed to fork
the target repos and open PRs — no write access to upstream is needed.
