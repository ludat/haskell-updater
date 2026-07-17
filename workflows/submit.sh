#!/usr/bin/env sh
# Submit one refactor run against a single Hackage package.
#
#   ./submit.sh heap-1.0.4 [--watch]
#
# Extra args after the package are passed through to `argo submit` (e.g. --watch,
# -p base-branch=master, -p run-tests=true).
set -eu

NAMESPACE="${NAMESPACE:-refactor-bot}"

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <hackage-package> [extra argo submit args...]" >&2
  echo "   e.g. $0 heap-1.0.4" >&2
  exit 1
fi

PACKAGE="$1"
shift

exec argo submit -n "$NAMESPACE" \
  --from workflowtemplate/haskell-refactor \
  -p package="$PACKAGE" \
  "$@"
