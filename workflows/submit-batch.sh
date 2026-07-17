#!/usr/bin/env sh
# Fire one workflow per Hackage package listed in a file (one package per line;
# blank lines and #-comments ignored). No CronWorkflow / controller — a submit
# loop is enough for an initial batch.
#
#   ./submit-batch.sh packages.txt
set -eu

NAMESPACE="${NAMESPACE:-refactor-bot}"
LIST="${1:-packages.txt}"

if [ ! -f "$LIST" ]; then
  echo "package list not found: $LIST" >&2
  exit 1
fi

while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|\#*) continue ;;
  esac
  echo ">>> submitting: $line"
  argo submit -n "$NAMESPACE" \
    --from workflowtemplate/haskell-refactor \
    -p package="$line"
done < "$LIST"
