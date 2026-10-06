#!/usr/bin/env bash
#
# render-backup-configmap.sh - regenerate base/backup-scripts-configmap.yaml from
# the scripts in this directory.
#
# The CronJobs cannot bake the scripts into an image without publishing one, and
# copying the script bodies into the ConfigMap by hand guarantees they will
# eventually drift from the files under version control. Rendering the manifest
# from the real files removes that failure mode: the script is the source of
# truth and the manifest is a build artifact.
#
#   ./render-backup-configmap.sh
#
# Run it after editing backup-databases.sh or restore-database.sh, and commit
# the result together with the change.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$here/../base/backup-scripts-configmap.yaml"

# Guard: a source file without a trailing newline in the YAML block scalar
# silently glues the next key onto the last line of the script. That corrupted
# the mounted backup-databases.sh (it ended with garbage instead of "BACKUP OK")
# and every backup Job failed with a syntax error at the very end. The
# defensive printf keeps the key on its own line no matter what the sources do.
emit() {
  printf '  %s: |\n' "$1"
  sed -e 's/^/    /' -e 's/[[:space:]]*$//' "$here/$1"
  printf '\n'
}

for script in backup-databases.sh restore-database.sh; do
  if [ ! -f "$here/$script" ]; then
    echo "missing $here/$script" >&2
    exit 1
  fi
done

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

{
  cat <<'HEADER'
# GENERATED FILE - do not edit by hand.
# Source: infrastructure/kubernetes/scripts/{backup-databases.sh,restore-database.sh}
# Regenerate with: infrastructure/kubernetes/scripts/render-backup-configmap.sh
#
# Both CronJobs in backup-cronjob.yaml mount this ConfigMap read-only, so the
# backup and restore logic that runs in the cluster is byte-identical to the
# version under version control.
apiVersion: v1
kind: ConfigMap
metadata:
  name: backup-scripts
  namespace: default
data:
HEADER
  emit backup-databases.sh
  emit restore-database.sh
} > "$tmp"

mv "$tmp" "$out"
trap - EXIT

# Self-check: the manifest must contain both script keys, each on its own line.
# A glued block (see the comment in emit) drops one of them and the mounted
# script is silently corrupt, so this is the cheapest real test the renderer
# can run on itself.
count="$(grep -cE '^  [a-z-]+\.sh: \|$' "$out" || true)"
if [ "$count" -ne 2 ]; then
  echo "render failed: expected 2 script keys in $out, got $count" >&2
  exit 1
fi

echo "wrote $out ($count script keys)"
