#!/usr/bin/env python3
"""Deterministic "Monad of No Return" refactor runner.

Replaces the LLM agent from ai/plan.md: instead of an agent loop, we already
have one precomputed git patch per Hackage package in ``auto.diff``.

The pipeline input is a Hackage package, so this program has two roles:

  * ``resolve`` -- map a Hackage package to its upstream git repo (fetch its
    .cabal from Hackage, read source-repository / homepage), for the clone+fork.

  * ``run`` -- against a single checked-out repo:
      1. Split ``auto.diff`` into per-package blocks and select this package's.
      2. Locate the package dir in the checkout (find the matching ``.cabal``;
         handles monorepo subdirs) and apply the diff there with ``git apply``.
      3. Verify it builds by running ``cabal build`` *in the build-server
         sidecar* over HTTP -- compiling third-party library code is untrusted,
         so it never runs in this container.
      4. Write a small ``result.json`` for the Argo step to surface.

Trust boundary
--------------
Applying the diff (steps 1-2) is deterministic and operates only on our own
patch text, so it runs locally in this container. Building (step 3) executes
arbitrary library code (custom Setup.hs, Template Haskell, build-type hooks)
and is therefore proxied over localhost to a separate build-server container in
the same pod, which shares the workspace volume but holds no credentials.

Stdlib only -- no pip install needed in the image.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- #
# Config (env with CLI overrides)
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "WORKDIR": "/workspace/repo",
    "DIFF_FILE": "/app/auto.diff",
    "RESULT_PATH": "/workspace/result.json",
    # cwd for the build in the sidecar. The sidecar mounts the same PVC subpath
    # at /workspace, so the discovered package dir path is valid there too.
    "BUILD_CWD": "",  # falls back to the discovered package dir when empty
}

HEADER_RE = re.compile(r"^# diff for (.+)$")
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, DEFAULTS.get(name, default))


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# auto.diff parsing
# --------------------------------------------------------------------------- #

def parse_diffs(path: str) -> dict[str, str]:
    """Split the combined patch file into ``{package-version: diff-text}``.

    Blocks are delimited by lines of the form ``# diff for <key>`` at column 0;
    a block's body is everything up to the next header, trimmed. Diff hunk lines
    start with ``+``/``-``/space, so a bare ``# diff for`` never collides with
    patch content.
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    diffs: dict[str, str] = {}
    key: str | None = None
    body: list[str] = []

    def flush() -> None:
        if key is not None:
            diffs[key] = "\n".join(body).strip("\n")

    for line in lines:
        match = HEADER_RE.match(line)
        if match:
            flush()
            key = match.group(1).strip()
            body = []
        elif key is not None:
            body.append(line)
    flush()
    return diffs


# --------------------------------------------------------------------------- #
# Package detection & diff selection
# --------------------------------------------------------------------------- #

SKIP_DIRS = {".git", "dist-newstyle", "dist", ".stack-work"}


def read_cabal(path: str) -> tuple[str | None, str | None]:
    """Read ``name`` and ``version`` from a specific .cabal file."""
    name = version = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            field = line.strip().lower()
            if name is None and field.startswith("name:"):
                name = line.split(":", 1)[1].strip()
            elif version is None and field.startswith("version:"):
                version = line.split(":", 1)[1].strip()
            if name and version:
                break
    return name, version


def find_package(root: str, want_name: str | None = None) -> tuple[str, str, str | None]:
    """Locate the package directory within a checkout (``root``).

    Hackage packages that live in a monorepo subdirectory rarely declare the
    ``subdir`` in their .cabal, so rather than trust that metadata we search the
    tree for the matching ``.cabal`` file. Returns ``(package_dir, name,
    version)``.

    With ``want_name``, the .cabal whose ``name:`` (or filename) matches wins,
    shallowest first. Without it, a single .cabal in the tree is used, otherwise
    the choice is ambiguous.
    """
    if not os.path.isdir(root):
        raise LookupError(f"checkout not found: {root}")

    found: list[tuple[int, int, str, str, str | None]] = []  # score, depth, dir, name, ver
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".cabal"):
                continue
            path = os.path.join(dirpath, fname)
            cname, cver = read_cabal(path)
            depth = os.path.relpath(dirpath, root).count(os.sep)
            if want_name:
                if cname == want_name:
                    score = 2
                elif fname[:-len(".cabal")] == want_name:
                    score = 1
                else:
                    continue
            else:
                score = 0
            found.append((score, depth, dirpath, cname or fname[:-len(".cabal")], cver))

    if not found:
        raise LookupError(
            f"no matching .cabal under {root}"
            + (f" for package '{want_name}'" if want_name else "")
            + "; pass --package / set PACKAGE"
        )
    if not want_name and len(found) > 1:
        names = ", ".join(sorted(f[3] for f in found))
        raise LookupError(
            f"multiple packages under {root} ({names}); set PACKAGE to disambiguate"
        )
    # Best score, then shallowest.
    found.sort(key=lambda t: (-t[0], t[1]))
    _, _, pkg_dir, name, version = found[0]
    return pkg_dir, name, version


def select_key(diffs: dict[str, str], name: str, version: str | None) -> str:
    """Pick the diff key for ``name``/``version``, with sensible fallbacks."""
    if version and f"{name}-{version}" in diffs:
        return f"{name}-{version}"
    # Fall back to any key whose leading segment is the package name and whose
    # remainder is a version string (handles a version mismatch between the
    # checkout and the patch set).
    prefix = f"{name}-"
    candidates = [
        k for k in diffs
        if k == name or (k.startswith(prefix) and VERSION_RE.match(k[len(prefix):]))
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise LookupError(
            f"no diff for package '{name}'"
            + (f" (version {version})" if version else "")
            + f"; available: {', '.join(sorted(diffs)[:5])}..."
        )
    raise LookupError(
        f"ambiguous diff for '{name}': {', '.join(sorted(candidates))}; "
        "set PACKAGE to the exact key"
    )


# --------------------------------------------------------------------------- #
# Resolve: Hackage package -> upstream source repository
# --------------------------------------------------------------------------- #

HACKAGE = "https://hackage.haskell.org"
PKG_KEY_RE = re.compile(r"^(.+)-([0-9]+(?:\.[0-9]+)*)$")
SRC_REPO_HEADER_RE = re.compile(r"^(\s*)source-repository\s+(\S+)", re.I)
FIELD_RE = re.compile(r"^\s*(location|subdir|type|tag|branch)\s*:\s*(.+?)\s*$", re.I)
HOMEPAGE_RE = re.compile(r"^\s*homepage\s*:\s*(.+?)\s*$", re.I)


def split_pkg_key(key: str) -> tuple[str, str | None]:
    """``heap-1.0.4`` -> (``heap``, ``1.0.4``); ``heap`` -> (``heap``, None)."""
    m = PKG_KEY_RE.match(key)
    return (m.group(1), m.group(2)) if m else (key, None)


def fetch_cabal(key: str) -> tuple[str, str, str | None]:
    """Fetch a package's .cabal from Hackage. Returns (text, name, version)."""
    name, version = split_pkg_key(key)
    # /package/<name>-<ver>/<name>.cabal for a pinned version, else latest.
    pkg_path = key if version else name
    url = f"{HACKAGE}/package/{pkg_path}/{name}.cabal"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace"), name, version


def normalize_git_url(loc: str) -> str:
    """Normalize a source-repository location to an https clone URL."""
    loc = loc.strip().rstrip("/")
    if loc.endswith(".git"):
        loc = loc[:-4]
    m = re.match(r"^git@([^:]+):(.+)$", loc)          # git@host:owner/name
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    loc = re.sub(r"^ssh://git@", "https://", loc)
    loc = re.sub(r"^git://", "https://", loc)
    loc = re.sub(r"^http://", "https://", loc)
    return loc


def parse_source_repo(cabal_text: str) -> tuple[str | None, str, str]:
    """Return (repo_url, subdir, vcs) from a .cabal's source-repository / homepage.

    Prefers a ``head`` stanza, then any other; falls back to a github-looking
    ``homepage:``. ``subdir`` is "" when the package is at the repo root; ``vcs``
    is the declared repository ``type`` (e.g. ``git``, ``darcs``) or "".
    """
    stanzas: list[dict[str, str]] = []
    homepage = None
    current: dict[str, str] | None = None
    base_indent = 0

    for line in cabal_text.splitlines():
        hp = HOMEPAGE_RE.match(line)
        if hp and homepage is None:
            homepage = hp.group(1).strip()

        header = SRC_REPO_HEADER_RE.match(line)
        if header:
            base_indent = len(header.group(1))
            current = {"kind": header.group(2).lower()}
            stanzas.append(current)
            continue

        if current is not None:
            if line.strip() == "":
                continue
            indent = len(line) - len(line.lstrip())
            field = FIELD_RE.match(line)
            if indent <= base_indent and not field:
                current = None            # dedented out of the stanza
                continue
            if field:
                current[field.group(1).lower()] = field.group(2)

    def pick() -> dict[str, str] | None:
        for kind in ("head", "this"):
            for s in stanzas:
                if s.get("kind") == kind and s.get("location"):
                    return s
        for s in stanzas:
            if s.get("location"):
                return s
        return None

    chosen = pick()
    if chosen:
        return (normalize_git_url(chosen["location"]),
                chosen.get("subdir", "").strip(),
                chosen.get("type", "").strip().lower())

    # Fallback: a github homepage is good enough to clone/fork.
    if homepage and re.search(r"github\.com|gitlab\.com|codeberg\.org", homepage, re.I):
        return normalize_git_url(homepage), "", "git"
    return None, "", ""


def resolve_package(key: str) -> dict:
    """Resolve a Hackage package key to its upstream repo."""
    cabal_text, name, version = fetch_cabal(key)
    repo, subdir, vcs = parse_source_repo(cabal_text)
    if not repo:
        raise LookupError(
            f"no source-repository or github homepage in {name}'s .cabal on Hackage"
        )
    if vcs and vcs != "git":
        # Only git can be forked/PR'd on GitHub; darcs/mercurial/svn can't.
        raise LookupError(
            f"{name} uses a non-git source repository ({vcs}: {repo}); skip it"
        )
    return {
        "package": key,
        "name": name,
        "version": version or "",
        "repo": repo,
        "vcs": vcs or "git",
        # Informational only; run-agent discovers the package dir by .cabal name.
        "subdir": subdir,
    }


# --------------------------------------------------------------------------- #
# Apply (local, trusted)
# --------------------------------------------------------------------------- #

def apply_diff(repo_root: str, rel_dir: str, diff_text: str) -> None:
    """Apply the patch, piped to ``git apply``, into ``rel_dir`` of the checkout.

    The patch paths in ``auto.diff`` are package-relative (``a/src/...``). For a
    ``diff --git`` patch, ``git apply`` resolves paths against the repo toplevel
    and *silently ignores* paths outside the cwd -- so for a package that lives
    in a monorepo subdirectory we must use ``--directory=<subdir>`` to prepend
    it (``.`` for a root package). Running ``git apply`` from the toplevel with
    ``--directory`` is the only reliable way to target a subdir.
    """
    patch = diff_text if diff_text.endswith("\n") else diff_text + "\n"

    dir_flag = ([f"--directory={rel_dir}"] if rel_dir and rel_dir != "." else [])

    for phase, extra in (("verify", ["--check"]), ("apply", ["--index"])):
        proc = subprocess.run(
            ["git", "-C", repo_root, "apply", "--whitespace=nowarn",
             *dir_flag, *extra, "-"],
            input=patch,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git apply --{phase} failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
            )


def diff_stat(diff_text: str) -> dict[str, int]:
    files = diff_text.count("\ndiff --git ") + diff_text.startswith("diff --git ")
    added = sum(
        1 for ln in diff_text.splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    )
    removed = sum(
        1 for ln in diff_text.splitlines()
        if ln.startswith("-") and not ln.startswith("---")
    )
    return {"files": int(files), "insertions": added, "deletions": removed}


# --------------------------------------------------------------------------- #
# Build (remote, untrusted -> build-server sidecar over HTTP)
# --------------------------------------------------------------------------- #

class ExecResult(dict):
    @property
    def ok(self) -> bool:
        return self.get("exitCode", 1) == 0


def wait_for_builder(builder_url: str, timeout: int = 120) -> None:
    """Block until the build-server sidecar answers /health.

    The sidecar and this container start together, with no ordering guarantee,
    so poll before sending the first command instead of racing it.
    """
    url = builder_url.rstrip("/") + "/health"
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(1)
    raise TimeoutError(f"build-server not ready at {url} after {timeout}s: {last}")


def builder_exec(builder_url: str, command: list[str], cwd: str, timeout: int) -> ExecResult:
    """Run ``command`` in the build-server sidecar via ``POST {url}/execute``.

    Contract (see buildserver/): the sidecar accepts ``{"command": [...], "cwd":
    ...}`` and returns ``{"stdout", "stderr", "exitCode"}``.
    """
    payload = json.dumps({"command": command, "cwd": cwd}).encode("utf-8")
    req = urllib.request.Request(
        builder_url.rstrip("/") + "/execute",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return ExecResult(data)


def run_build(builder_url: str, cwd: str, run_tests: bool, do_update: bool,
              timeout: int) -> dict:
    """Drive cabal in the build-server sidecar; return a structured build report."""
    steps: list[dict] = []

    def step(name: str, command: list[str], required: bool) -> bool:
        print(f"[build] {name}: {' '.join(command)}", flush=True)
        res = builder_exec(builder_url, command, cwd, timeout)
        if res.get("stdout"):
            print(res["stdout"], end="", flush=True)
        if res.get("stderr"):
            print(res["stderr"], end="", file=sys.stderr, flush=True)
        steps.append({"name": name, "exit_code": res.get("exitCode", 1),
                      "required": required})
        if not res.ok and required:
            return False
        return True

    if do_update:
        # Best-effort: a stale-but-present index is fine, so don't fail on this.
        step("cabal update", ["cabal", "update"], required=False)

    ok = step("cabal build", ["cabal", "build", "all"], required=True)
    tests_ok = None
    if ok and run_tests:
        tests_ok = step("cabal test", ["cabal", "test", "all"], required=True)

    return {
        "ok": ok and (tests_ok is not False),
        "build_ok": ok,
        "tests_ok": tests_ok,
        "steps": steps,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def write_result(path: str, result: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print(f"[result] wrote {path}", flush=True)
    except OSError as exc:
        print(f"[result] could not write {path}: {exc}", file=sys.stderr, flush=True)


def cmd_run(args: argparse.Namespace) -> int:
    root = args.workdir
    result_path = args.result
    result: dict = {"package": None, "matched_key": None, "package_dir": None,
                    "applied": False, "build": None, "status": "error"}

    diffs = parse_diffs(args.diff_file)

    # Locate the package directory in the checkout (handles monorepo subdirs by
    # finding the matching .cabal rather than trusting cabal 'subdir' metadata).
    want_name = split_pkg_key(args.package)[0] if args.package else None
    try:
        pkg_dir, name, version = find_package(root, want_name)
    except LookupError as exc:
        result["status"] = "package-not-found"
        result["error"] = str(exc)
        print(f"[select] FAILED: {exc}", file=sys.stderr, flush=True)
        write_result(result_path, result)
        return 1
    result["package_dir"] = pkg_dir

    # Select the diff: an explicit PACKAGE that is already a diff key wins;
    # otherwise match the discovered package name/version.
    if args.package and args.package in diffs:
        key = args.package
    else:
        key = select_key(diffs, want_name or name, version)
    result["package"] = args.package or (f"{name}-{version}" if version else name)
    result["matched_key"] = key
    print(f"[select] package={name} version={version or '?'} dir={pkg_dir} -> diff '{key}'",
          flush=True)
    result["diff_stat"] = diff_stat(diffs[key])

    # Apply (trusted, local) -- into the discovered package dir. git apply runs
    # from the checkout root with --directory so subdir packages patch correctly.
    rel_dir = os.path.relpath(pkg_dir, root)
    try:
        apply_diff(root, rel_dir, diffs[key])
    except RuntimeError as exc:
        result["status"] = "apply-failed"
        result["error"] = str(exc)
        print(f"[apply] FAILED: {exc}", file=sys.stderr, flush=True)
        write_result(result_path, result)
        return 2
    result["applied"] = True
    print(f"[apply] applied '{key}' ({result['diff_stat']})", flush=True)

    if args.skip_build:
        result["status"] = "applied-no-build"
        write_result(result_path, result)
        return 0

    # Build (untrusted, in the build-server sidecar).
    if not args.builder_url:
        result["status"] = "no-builder"
        result["error"] = "BUILDER_URL not set; cannot build untrusted code safely"
        print("[build] BUILDER_URL not set -- refusing to build locally", file=sys.stderr)
        write_result(result_path, result)
        return 3

    # The sidecar shares this same mount path, so the discovered package dir is
    # a valid cwd there too.
    build_cwd = args.build_cwd or pkg_dir
    try:
        wait_for_builder(args.builder_url, args.builder_timeout)
        build = run_build(args.builder_url, build_cwd, args.run_tests,
                          args.cabal_update, args.timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        result["status"] = "builder-error"
        result["error"] = f"builder exec failed: {exc}"
        print(f"[build] builder error: {exc}", file=sys.stderr, flush=True)
        write_result(result_path, result)
        return 4

    result["build"] = build
    result["status"] = "success" if build["ok"] else "build-failed"
    write_result(result_path, result)
    print(f"[done] status={result['status']}", flush=True)
    return 0 if build["ok"] else 5


def cmd_list(args: argparse.Namespace) -> int:
    for key in sorted(parse_diffs(args.diff_file)):
        print(key)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    diffs = parse_diffs(args.diff_file)
    key = args.package if args.package in diffs else select_key(diffs, args.package, None)
    print(diffs[key])
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    """Resolve a Hackage package to its upstream repo, for the clone/fork steps.

    Prints the JSON to stdout and, when --out-dir is given, writes one file per
    field (repo, name, version, vcs, subdir) so an Argo step can expose them as
    output parameters via ``valueFrom.path``.
    """
    info = resolve_package(args.package)
    print(json.dumps(info, indent=2))
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        for field in ("repo", "name", "version", "vcs", "subdir"):
            with open(os.path.join(args.out_dir, field), "w", encoding="utf-8") as fh:
                fh.write(str(info[field]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--diff-file", default=env("DIFF_FILE"),
                        help="combined patch file (default: %(default)s)")

    # NOTE: --diff-file lives only on the subparsers (via `common`), never on
    # the top-level parser, otherwise a subparser's default clobbers the value
    # parsed before the subcommand. main() injects the `run` subcommand when
    # none is given, so bare `--diff-file ...` still reaches a subparser.
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common],
                         help="select+apply diff, then build in the sidecar (default)")
    run.add_argument("--workdir", default=env("WORKDIR"))
    run.add_argument("--package", default=os.environ.get("PACKAGE", ""),
                     help="override package/diff key (default: auto-detect from .cabal)")
    run.add_argument("--builder-url", default=os.environ.get("BUILDER_URL", ""),
                     help="build-server sidecar base URL, e.g. http://localhost:8080")
    run.add_argument("--builder-timeout", type=int,
                     default=int(os.environ.get("BUILDER_READY_TIMEOUT", "120")),
                     help="seconds to wait for the sidecar /health before building")
    run.add_argument("--build-cwd", default=env("BUILD_CWD"),
                     help="cwd for the build in the sidecar (default: discovered package dir)")
    run.add_argument("--result", default=env("RESULT_PATH"))
    run.add_argument("--run-tests", action="store_true",
                     default=truthy(os.environ.get("RUN_TESTS", "")))
    run.add_argument("--no-cabal-update", dest="cabal_update", action="store_false",
                     default=not truthy(os.environ.get("SKIP_CABAL_UPDATE", "")))
    run.add_argument("--skip-build", action="store_true",
                     default=truthy(os.environ.get("SKIP_BUILD", "")),
                     help="apply only; do not build (local testing)")
    run.add_argument("--timeout", type=int,
                     default=int(os.environ.get("BUILD_TIMEOUT", "3600")))
    run.set_defaults(func=cmd_run)

    lst = sub.add_parser("list", parents=[common],
                         help="list all package keys in the diff file")
    lst.set_defaults(func=cmd_list)

    ext = sub.add_parser("extract", parents=[common],
                         help="print one package's diff to stdout")
    ext.add_argument("package")
    ext.set_defaults(func=cmd_extract)

    res = sub.add_parser("resolve", parents=[common],
                         help="resolve a Hackage package to its upstream repo + subdir")
    res.add_argument("package", help="Hackage package key, e.g. heap-1.0.4 or heap")
    res.add_argument("--out-dir", default=os.environ.get("RESOLVE_OUT_DIR", ""),
                     help="write one file per field here (for Argo output params)")
    res.set_defaults(func=cmd_resolve)

    return parser


KNOWN_COMMANDS = {"run", "list", "extract", "resolve"}


def main(argv: list[str]) -> int:
    parser = build_parser()
    # Default to the `run` subcommand when none is given (e.g. bare `docker run`).
    if not any(tok in KNOWN_COMMANDS for tok in argv):
        argv = ["run", *argv]
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LookupError as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"[error] Hackage lookup failed: {exc}", file=sys.stderr, flush=True)
        return 6


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
