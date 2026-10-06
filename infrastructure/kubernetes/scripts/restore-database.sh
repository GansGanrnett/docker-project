#!/usr/bin/env bash
#
# restore-database.sh - restore a database from a verified backup.
#
#   restore-database.sh postgres   [--target TIMESTAMP] [--yes]
#   restore-database.sh mongodb    [--target TIMESTAMP] [--yes]
#
# This is the other half of backup-databases.sh and it is deliberately hard to
# run by accident: a restore replaces the live contents of the database, and the
# most common way to lose data is a restore that was fired without thinking about
# which archive it would pick. So the script prints the archive it resolved, the
# manifest status and the target database, and refuses to touch anything without
# an explicit --yes.
#
# The archive checksums recorded by the backup script are re-verified before use,
# so a truncated file on the PVC is reported instead of being half-applied.
#
# Image capabilities this script relies on (verified against both images):
#   postgres:15-alpine  bash, sha256sum, pg_restore
#   mongo:6.0           bash, sha256sum, mongorestore
# Note that mongo:6.0 has no `nc`, so readiness is probed over bash's own
# /dev/tcp rather than shelling out to netcat.
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/backups}"

# log/red write to stderr on purpose. resolve_dir() and the per-target lookups
# run inside command substitutions, where stdout is captured as the returned
# value - anything printed there would be swallowed into the variable and the
# operator would never see why the restore stopped. stderr is not captured, so
# the diagnostic always reaches the pod log.
log()   { printf '%s\n' "  $*" >&2; }
red()   { printf '\033[31m%s\033[0m\n' "$*" >&2; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

usage() {
  red "usage: restore-database.sh <postgres|mongodb> [--target TIMESTAMP] [--yes]"
  exit 2
}

# The mode is positional and comes first, so it is read before option parsing -
# otherwise the loop below consumes it and every invocation ends in "usage".
MODE="${1:-}"
[ $# -gt 0 ] && shift
[ -n "$MODE" ] || usage

target_ts=""
assume_yes=0
while [ $# -gt 0 ]; do
  case "$1" in
    --target)
      target_ts="${2:-}"
      [ -n "$target_ts" ] || { red "--target requires a timestamp"; exit 2; }
      shift 2
      ;;
    --yes|-y) assume_yes=1; shift ;;
    -h|--help) usage ;;
    *) red "unknown argument: $1"; usage ;;
  esac
done

# --- configuration -----------------------------------------------------------
: "${AUTH_DB_HOST:=auth-postgres-service}"
: "${AUTH_DB_PORT:=5432}"
: "${AUTH_DB_NAME:=auth_db}"
: "${CATALOG_DB_HOST:=catalog-postgres-service}"
: "${CATALOG_DB_PORT:=5432}"
: "${CATALOG_DB_NAME:=catalog_db}"
: "${PAYMENT_DB_HOST:=payment-postgres-service}"
: "${PAYMENT_DB_PORT:=5432}"
: "${PAYMENT_DB_NAME:=payment_db}"
: "${MONGO_HOST:=order-mongodb-service}"
: "${MONGO_PORT:=27017}"
: "${MONGO_DB:=order_db}"

export PGPASSFILE=/dev/null

# Only the credentials the selected mode actually uses are required, matching how
# backup-databases.sh validates: a mongodb restore must not fail because no
# PostgreSQL secret was mounted into the Job.
require_pg_credentials() {
  local name value missing=""
  for name in AUTH_DB_USER AUTH_DB_PASSWORD CATALOG_DB_USER CATALOG_DB_PASSWORD \
              PAYMENT_DB_USER PAYMENT_DB_PASSWORD; do
    eval "value=\${$name:-}"
    [ -n "$value" ] || missing="$missing $name"
  done
  if [ -n "$missing" ]; then
    red "missing required environment for mode '$MODE':$missing"
    exit 1
  fi
}

# Uses bash's /dev/tcp so the script needs no netcat, which mongo:6.0 lacks.
tcp_wait() {
  local host="$1" port="$2" label="$3" _
  for _ in $(seq 1 30); do
    if (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  red "$label: $host:$port did not accept connections within 60s"
  return 1
}

# Resolves the archive directory: the requested one, or the newest *verified*
# backup. backup-databases.sh keeps failed directories around for forensics, so
# "newest directory" is not the same as "newest backup that will actually restore".
resolve_dir() {
  local target="$1" dir candidate
  if [ -n "$target_ts" ]; then
    dir="$BACKUP_ROOT/$target/$target_ts"
    [ -d "$dir" ] || { red "no backup at $dir"; exit 1; }
    printf '%s' "$dir"
    return
  fi
  while IFS= read -r candidate; do
    if [ -f "$candidate/manifest.txt" ] && grep -qx 'verified=OK' "$candidate/manifest.txt"; then
      printf '%s' "$candidate"
      return
    fi
    log "skipping ${candidate##*/} (no verified backup)"
  done < <(find "$BACKUP_ROOT/$target" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort -r)
  red "no verified backups found under $BACKUP_ROOT/$target - run backup-databases.sh first"
  exit 1
}

# Re-verifies every checksum in the manifest. Only lines below the "--- sha256 ---"
# marker are checksums: the metadata lines above it are not "<hash>  <file>" pairs
# and must not be parsed as if they were. Parsing the whole file makes every
# restore fail on the manifest's own target= line.
check_manifest() {
  # Two separate local declarations on purpose: within one `local` statement the
  # later assignment still sees the *outer* dir, so manifest would be built from
  # the previous target's path.
  local dir="$1"
  local manifest="$dir/manifest.txt"
  if [ ! -f "$manifest" ]; then
    red "manifest missing: $manifest"
    exit 1
  fi

  local status
  status="$(sed -n 's/^verified=//p' "$manifest")"
  if [ "$status" != "OK" ]; then
    red "backup $dir is marked verified=$status - refusing to restore from it"
    exit 1
  fi

  # The PVC can hold a truncated file after a disk problem, and that is exactly
  # when someone reaches for a restore.
  local line file want got in_section=0 checked=0
  while IFS= read -r line; do
    case "$line" in
      '--- sha256 ---') in_section=1; continue ;;
    esac
    [ "$in_section" -eq 1 ] || continue
    [ -n "$line" ] || continue
    # sha256sum prints "<hash><space><space><path>".
    want="${line%% *}"
    file="${line#* }"
    file="${file# }"
    if [ ! -f "$file" ]; then
      red "archive listed in the manifest is missing: $file"
      exit 1
    fi
    got="$(sha256sum "$file" | awk '{print $1}')"
    if [ "$got" != "$want" ]; then
      red "checksum mismatch for $file"
      red "  expected $want"
      red "  actual   $got"
      exit 1
    fi
    checked=$((checked + 1))
  done < "$manifest"

  [ "$checked" -gt 0 ] || { red "manifest lists no checksums: $manifest"; exit 1; }
  log "checksum verified ($checked files)"
}

confirm() {
  [ "$assume_yes" -eq 1 ] && return 0
  red "REFUSING TO RUN WITHOUT --yes"
  log "this replaces the live contents of: $*"
  exit 1
}

case "$MODE" in
  postgres)
    require_pg_credentials
    # Two phases on purpose. Every archive is resolved and verified before
    # anything is restored, so a corrupt catalog archive cannot leave auth
    # dropped-and-empty while catalog was never touched - two half-restored
    # databases are strictly worse than not having started.
    labels=(); hosts=(); ports=(); dbs=(); users=(); pwds=(); archives=(); dirs=()
    for pair in "auth-postgres|$AUTH_DB_HOST|$AUTH_DB_PORT|$AUTH_DB_NAME|$AUTH_DB_USER|$AUTH_DB_PASSWORD" \
                "catalog-postgres|$CATALOG_DB_HOST|$CATALOG_DB_PORT|$CATALOG_DB_NAME|$CATALOG_DB_USER|$CATALOG_DB_PASSWORD" \
                "payment-postgres|$PAYMENT_DB_HOST|$PAYMENT_DB_PORT|$PAYMENT_DB_NAME|$PAYMENT_DB_USER|$PAYMENT_DB_PASSWORD"; do
      IFS='|' read -r label host port db user password <<< "$pair"

      # `set -e` does not fire inside a command substitution, so resolve_dir's
      # `exit 1` would only end the subshell and leave $dir empty - the script
      # would then walk on with an empty path instead of stopping. The exit
      # status has to be checked explicitly.
      dir="$(resolve_dir "$label")" || { red "no usable backup for $label"; exit 1; }
      [ -n "$dir" ] || { red "resolve_dir returned an empty path for $label"; exit 1; }
      archive="$(find "$dir" -name '*.dump' -type f | head -1)"
      [ -n "$archive" ] || { red "no .dump archive in $dir"; exit 1; }

      echo "=== $label ==="
      log "archive : $archive"
      log "size    : $(wc -c < "$archive") bytes"
      log "target  : $db@$host:$port as $user"
      check_manifest "$dir"
      tcp_wait "$host" "$port" "$label" || exit 1

      labels+=("$label"); hosts+=("$host"); ports+=("$port")
      dbs+=("$db"); users+=("$user"); pwds+=("$password"); archives+=("$archive"); dirs+=("$dir")
    done

    for i in "${!labels[@]}"; do
      confirm "${dbs[$i]}@${hosts[$i]}:${ports[$i]}"
      # Exported so pg_restore inherits it; a CLI flag would expose it in `ps`.
      export PGPASSWORD="${pwds[$i]}"
      # --clean --if-exists drops the existing objects before recreating them.
      # --single-transaction keeps the restore all-or-nothing: without it a
      # failure halfway through leaves a partially dropped schema behind, which
      # is worse than the state we started from.
      if pg_restore --clean --if-exists --no-owner --single-transaction \
                    -h "${hosts[$i]}" -p "${ports[$i]}" -U "${users[$i]}" \
                    -d "${dbs[$i]}" "${archives[$i]}"; then
        green "    ${labels[$i]} restored from ${dirs[$i]##*/}"
      else
        red "    ${labels[$i]} restore FAILED"
        exit 1
      fi
    done
    ;;
  mongodb)
    dir="$(resolve_dir mongodb)" || { red "no usable mongodb backup"; exit 1; }
    [ -n "$dir" ] || { red "resolve_dir returned an empty path for mongodb"; exit 1; }
    bson="$(find "$dir" -name '*.bson' -type f | wc -l)"
    if [ "$bson" -eq 0 ]; then
      red "mongodb dump contains no collections - nothing to restore"
      exit 1
    fi

    echo "=== mongodb ==="
    log "archive : $dir ($bson bson files)"
    log "target  : $MONGO_DB@$MONGO_HOST:$MONGO_PORT"
    check_manifest "$dir"
    tcp_wait "$MONGO_HOST" "$MONGO_PORT" mongodb || exit 1
    confirm "$MONGO_DB@$MONGO_HOST:$MONGO_PORT"

    # The dump root is passed, NOT $dir/$MONGO_DB: mongodump writes
    # <root>/<db>/<collection>.bson and mongorestore expects that same root.
    # Pointing it at the database subdirectory makes it answer "don't know what
    # to do with file ... skipping" for every file and restore nothing at all -
    # a silent no-op that looks like a successful restore.
    # --nsInclude keeps --drop inside the dumped namespace.
    if mongorestore --drop --nsInclude "$MONGO_DB.*" \
                    --host "$MONGO_HOST" --port "$MONGO_PORT" "$dir"; then
      green "    mongodb restored from ${dir##*/}"
    else
      red "    mongodb restore FAILED"
      exit 1
    fi
    ;;
  *)
    red "unknown mode: $MODE (expected postgres or mongodb)"
    exit 2
    ;;
esac

green "RESTORE OK"
