# run-agent — deterministic "Monad of No Return" patch runner

Replaces the LLM agent: instead of an agent loop, every target package already
has a precomputed git patch in [`../auto.diff`](../auto.diff) (one block per
Hackage package, delimited by `# diff for <name-version>` lines).
[`refactor.py`](refactor.py) has two roles.

**`resolve`** — maps a Hackage package to its upstream git repo (used by the
Argo `resolve-repo` step): fetches the package's `.cabal` from Hackage, reads
the `source-repository` location (normalizing `git://` / `git@` to https),
falls back to a github `homepage:`, and rejects non-git repos (darcs, etc.).

**`run`** — against one checked-out repo:

1. **Locates** the package dir in the checkout by finding the `.cabal` whose
   `name:` matches (so a package inside a monorepo subdirectory patches in the
   right place — cabal's `subdir` metadata is often missing, so we don't trust
   it). Diff key comes from `--package` / `PACKAGE`, else the discovered package.
2. **Applies** the diff with `git apply --directory=<pkgdir>` (patch on stdin) —
   `--directory` is required so subdir packages patch correctly, since a
   `diff --git` patch resolves paths against the repo toplevel.
3. **Builds** by running `cabal build all` (and optionally `cabal test all`) in
   the **build-server sidecar** over HTTP, with cwd set to the package dir —
   never locally. It first polls the sidecar's `/health` to avoid racing its
   startup.
4. **Writes** `result.json` (`{package, matched_key, package_dir, applied,
   build, status, diff_stat}`) for the Argo step to surface, and exits non-zero
   on failure.

### Trust boundary

Applying our own patch is deterministic and safe, so it runs in this container.
`cabal build` compiles third-party library code (custom `Setup.hs`, Template
Haskell, build-type hooks all execute arbitrary code) — untrusted — so it is
proxied over `localhost` to the [build-server sidecar](../buildserver/), a
separate container in the same pod that shares the workspace volume but holds no
credentials. This container has no k8s token and no secrets.

### Builder exec contract

`cabal` commands are sent as `POST {BUILDER_URL}/execute` with
`{"command": [...], "cwd": ...}`, expecting `{"stdout", "stderr", "exitCode"}`;
readiness is checked with `GET /health`. See [`../buildserver/`](../buildserver/).

## Usage

Stdlib only — no `pip install`.

```sh
# Inside the pod (env-driven, as Argo runs it):
python3 refactor.py resolve heap-1.0.4 --out-dir /tmp/resolve   # -> repo, name, ...
python3 refactor.py run          # WORKDIR, PACKAGE, BUILDER_URL, DIFF_FILE from env

# Locally against a checkout:
python3 refactor.py run --workdir ./repo --package heap-1.0.4 --diff-file ../auto.diff \
  --builder-url http://localhost:8080 [--run-tests] [--skip-build]

python3 refactor.py list --diff-file ../auto.diff       # all package keys
python3 refactor.py extract heap-1.0.4 --diff-file ../auto.diff
python3 refactor.py resolve heap-1.0.4                   # print repo JSON
```

| env | flag | default | meaning |
| --- | --- | --- | --- |
| `WORKDIR` | `--workdir` | `/workspace/repo` | checkout root (package dir found under it) |
| `DIFF_FILE` | `--diff-file` | `/app/auto.diff` | combined patch set |
| `PACKAGE` | `--package` | auto from `.cabal` | diff key + which package dir to find |
| `BUILDER_URL` | `--builder-url` | — | build-server sidecar base URL (e.g. `http://localhost:8080`) |
| `BUILDER_READY_TIMEOUT` | `--builder-timeout` | `120` | seconds to wait for the sidecar `/health` |
| `BUILD_CWD` | `--build-cwd` | discovered package dir | cwd for the build in the sidecar |
| `RESULT_PATH` | `--result` | `/workspace/result.json` | result blob path |
| `RUN_TESTS` | `--run-tests` | off | also run `cabal test all` |
| `SKIP_BUILD` | `--skip-build` | off | apply only (local testing) |
| `SKIP_CABAL_UPDATE` | `--no-cabal-update` | runs `cabal update` | skip the index refresh |
| `RESOLVE_OUT_DIR` | `--out-dir` | — | (resolve) write one file per field here |

Exit codes: `0` ok · `1` selection/resolve error · `2` apply failed · `3` no
builder · `4` builder unreachable · `5` build/test failed · `6` Hackage lookup
failed.

## Image

Built from the **repo root** (so `auto.diff` is in context):

```sh
docker build -f agent/Dockerfile -t ghcr.io/<owner>/refactor-agent:latest .
```

python3 + git only — no GHC/cabal (the toolchain lives in the build sidecar).

CI publishes it (and the build-server) automatically:
[`.github/workflows/build-images.yml`](../.github/workflows/build-images.yml)
pushes `ghcr.io/<owner>/refactor-agent` and `refactor-build-server` (tags:
`latest`, `sha-<commit>`, `vX.Y.Z` on git tags) on every push to `main` that
touches `agent/**`, `buildserver/**`, or `auto.diff`, using `GITHUB_TOKEN`.

## Local dev

`nix develop` (see [`../flake.nix`](../flake.nix)) provides python3, git, and go.
