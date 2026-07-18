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
resolve-repo ─► clone-repo ─► run-agent ─► push-branch ─► [await-approval] ─► open-pr

onExit: delete-workspace   (always runs)
```

`resolve-repo` fetches the package's `.cabal` from public Hackage and reads its
`source-repository` to get the upstream git URL (non-git repos like darcs are
rejected). The clone, agent, and push steps share one JuiceFS-backed RWX PVC,
each subPathed under the workflow name, so they all operate on the same on-disk
checkout.

`push-branch` forks the upstream repo (`gh repo fork`) and pushes the branch
there — so the bot never needs write access to the target repos. It does **not**
open the PR. By default the fork lands in the bot user's own account; set
`fork-org=<org>` to fork into a GitHub organization instead (handy when several
people need to open/review the PR later — the token's account must be able to
create repos in that org).

### When the PR is opened — `pr-mode`

The branch is always pushed to the fork; the `pr-mode` parameter decides what
happens next:

| `pr-mode`        | Behaviour |
| ---------------- | --------- |
| `manual` (default) | Workflow **suspends** at `await-approval`; the PR opens only when you `argo resume <workflow-name>` (or click Resume in the UI). Until then the branch sits on the fork with no PR — `argo stop` it to abandon. |
| `auto`           | The PR opens immediately after the push, no waiting. |
| `skip`           | No PR is ever opened — the branch is just left on the fork. |

```sh
./submit.sh heap-1.0.4                     # manual (default): push, then wait
./submit.sh heap-1.0.4 -p pr-mode=auto     # push + open PR right away
./submit.sh heap-1.0.4 -p pr-mode=skip     # push only
```

Regardless of mode, `open-pr` is skipped automatically when the agent produced no
changes (nothing to PR).

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

**Shared cabal cache.** The sidecar sets `CABAL_DIR=/cabal`, mounted from a
constant `cabal-cache` subpath of the workspace PVC (not the per-run subpath). So
cabal's Hackage index and nix-style store of compiled dependencies persist and
are shared across every run — a dependency is downloaded and compiled once, then
reused. It's never cleared by `delete-workspace`. cabal's store uses per-package
locks, so concurrent runs can share it safely.

## Credential isolation

No application container touches the Kubernetes API — there are no resource
steps, and no token is mounted in any main container. The SA's only RBAC is the
standard Argo executor permission (`workflowtaskresults` create/patch), which the
executor sidecar needs to report each step's result; it grants nothing else.

| Step                    | git token | secrets |
| ----------------------- | --------- | ------- |
| resolve-repo            |           | **none** |
| clone-repo              | ✓         |         |
| run-agent (+ sidecar)   |           | **none** |
| push-branch             | ✓ (fork + push) |   |
| await-approval          |           | **none** (suspend) |
| open-pr                 | ✓ (PR)    |         |

**Every** step runs as the single `refactor-agent` ServiceAccount with
`automountServiceAccountToken: false` (set once at the workflow spec level), so
no step's main container gets a token and none can reach the API server. The SA's
token is mounted only into Argo's executor sidecar (via
`executor.serviceAccountName`), and its lone permission is the Argo-required
`workflowtaskresults` create/patch used to report step results — nothing else.
The git token is mounted only in the clone, push, and open-pr steps.

## Files

| File                     | Purpose                                            |
| ------------------------ | -------------------------------------------------- |
| `namespace.yaml`         | `refactor-bot` namespace                           |
| `workspace-pvc.yaml`     | JuiceFS RWX PVC shared by every run                |
| `rbac.yaml`              | SA + minimal Argo executor role (task-result only) |
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
./submit.sh heap-1.0.4 -p fork-org=my-org      # fork into an org instead of the bot user
# or a batch (one Hackage package per line):
cp packages.example.txt packages.txt && $EDITOR packages.txt
./submit-batch.sh packages.txt
```

The bot's GitHub account (the `git-credentials` token) must be allowed to fork
the target repos and open PRs — no write access to upstream is needed. To fork
into an organization (`fork-org`), the token's account must also be able to
create repositories in that org.
