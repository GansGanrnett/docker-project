#!/usr/bin/env bash
#
# backup-databases.sh - dump the stateful workloads of the single-node cluster.
#
# Called from the two CronJobs in base/backup-cronjob.yaml:
#   backup-postgres  -> postgres:15-alpine, mode "postgres"
#   backup-mongodb   -> mongo:6.0,          mode "mongodb"
#
# One script for both images so the layout, the retention rule and the
# verification logic exist in exactly one place. Each mode only touches the
# tooling its image actually has.
#
# Output layout on the backups PVC:
#   /backups/<target>/<UTC timestamp>/
#       <file>.dump            pg_dump -Fc archive
#       order_db/*.bson        mongodump directory (per-collection payloads)
#       manifest.txt           sha256, size, verification result
#
# A backup that was never restored is not a backup, so every archive is verified
# before it is recorded as OK: pg_restore --list must parse the archive and report
# entries, and a mongodump directory must contain the metadata.json that
# mongodump writes on completion. A failing target makes the script exit non-zero
# so the CronJob shows it instead of quietly producing nothing.
#
# Image capabilities this script relies on (verified against both images):
#   postgres:15-alpine  bash, sha256sum, pg_dump, pg_restore
#   mongo:6.0           bash, sha256sum, mongodump, mongorestore
# Note that mongo:6.0 has no `nc`, so readiness is probed over bash's own
# /dev/tcp rather than shelling out to netcat.
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/backups}"
RETENTION="${RETENTION:-7}"
MODE="${1:-all}"

log()   { printf '%s\n' "  $*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

# --- configuration -----------------------------------------------------------
# Credentials are injected from the app-secrets Secret, never passed on the
# command line: an argument is visible in `ps` to every container in the pod.
: "${AUTH_DB_HOST:=auth-postgres-service}"
: "${AUTH_DB_PORT:=5432}"
: "${AUTH_DB_NAME:=auth_db}"
: "${CATALOG_DB_HOST:=catalog-postgres-service}"
: "${CATALOG_DB_PORT:=5432}"
: "${CATALOG_DB_NAME:=catalog_db}"
: "${MONGO_HOST:=order-mongodb-service}"
: "${MONGO_PORT:=27017}"
# Scoped to the application database. A full-server dump would also capture the
# admin/system databases, which mongorestore --drop then refuses to recreate -
# turning a routine restore into a failure at the last step.
: "${MONGO_DB:=order_db}"

export PGPASSFILE=/dev/null

fail=0

timestamp() { date -u +%Y%m%dT%H%M%SZ; }

# Each mode is validated only for the credentials it actually uses. The mongodb
# CronJob intentionally ships no PostgreSQL secrets, so validating all of them up
# front would abort that job before it touched anything.
require_pg_credentials() {
  local name value missing=""
  for name in AUTH_DB_USER AUTH_DB_PASSWORD CATALOG_DB_USER CATALOG_DB_PASSWORD; do
    eval "value=\${$name:-}"
    [ -n "$value" ] || missing="$missing $name"
  done
  if [ -n "$missing" ]; then
    red "missing required environment for mode '$MODE':$missing"
    exit 1
  fi
}

# Waits for a TCP listener. A CronJob pod is scheduled independently of the
# database pods, so "the host is in DNS" does not mean "postgres is accepting
# connections yet" - without this the first scheduled run of the day fails
# whenever the databases happen to be restarting. Uses bash's /dev/tcp so the
# script needs no netcat, which mongo:6.0 does not ship.
tcp_wait() {
  local host="$1" port="$2" label="$3" _
  for _ in $(seq 1 30); do
    # The subshell owns fd 3 and closes it on exit, so no descriptor leaks.
    if (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  red "    $label: $host:$port did not accept connections within 60s"
  return 1
}

# A directory counts as usable only if its manifest says so. Failed runs leave
# their directory behind on purpose - the FAILED manifest is the only forensic
# record of what went wrong - so "newest directory" and "newest good backup" are
# not the same thing.
is_verified() {
  local manifest="$1/manifest.txt"
  [ -f "$manifest" ] && grep -qx 'verified=OK' "$manifest"
}

write_manifest() {
  local dir="$1" target="$2" result="$3" database="$4" file
  {
    echo "target=$target"
    echo "database=$database"
    echo "timestamp=$(basename "$dir")"
    echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "mode=$MODE"
    echo "verified=$result"
    echo "--- sha256 ---"
    # Hash every payload file, not just *.dump. A mongodump directory is a set of
    # .bson files plus metadata.json, and an un-hashed .bson is exactly what a
    # truncated write on a full disk damages.
    while IFS= read -r file; do
      sha256sum "$file"
    done < <(find "$dir" -type f ! -name manifest.txt | sort)
  } > "$dir/manifest.txt"
}

# Keeps the newest $RETENTION *verified* backups of a target and deletes the rest.
# Counting failed directories toward the limit would let a target that keeps
# failing slowly push its last good backups off the PVC, which is the opposite
# of what retention is for.
apply_retention() {
  local target="$1" keep="$2" dir cutoff
  local -a verified=()
  while IFS= read -r dir; do
    is_verified "$dir" && verified+=("$dir")
  done < <(find "$BACKUP_ROOT/$target" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort -r)

  if [ "${#verified[@]}" -le "$keep" ]; then
    return 0
  fi

  # Delete everything strictly older than the oldest backup still being retained.
  # That reclaims the FAILED directories too, without ever pruning a good one.
  # The comparison must be strict: the retained one is included in the window, so
  # '<=' would delete the oldest surviving backup as well and keep RETENTION-1
  # good copies (with RETENTION=1 that empties the target completely).
  cutoff="${verified[$((keep - 1))]}"
  while IFS= read -r dir; do
    log "    pruning ${dir##*/}"
    rm -rf "$dir"
  done < <(find "$BACKUP_ROOT/$target" -mindepth 1 -maxdepth 1 -type d | sort -r | awk -v c="$cutoff" '$0 < c')
}

backup_postgres_target() {
  local label="$1" host="$2" port="$3" db="$4" user="$5" password="$6"
  local ts dir archive entries

  # Exported so pg_dump inherits it; a CLI flag would expose it in `ps`.
  export PGPASSWORD="$password"
  ts="$(timestamp)"
  dir="$BACKUP_ROOT/$label/$ts"
  archive="$dir/$label-$ts.dump"
  mkdir -p "$dir"

  log "$label: dumping $db@$host:$port"
  if ! tcp_wait "$host" "$port" "$label"; then
    fail=1
    return
  fi

  if ! pg_dump -Fc -h "$host" -p "$port" -U "$user" -d "$db" -f "$archive"; then
    red "    $label: pg_dump failed"
    fail=1
    return
  fi

  # Verification: pg_restore --list parses the custom-format archive and prints
  # its table of contents. It fails on a truncated or corrupt file, which is
  # exactly the failure mode that makes a backup useless.
  if entries="$(pg_restore --list "$archive" 2>/dev/null)" && [ -n "$entries" ]; then
    green "    $label: archive verified ($(printf '%s\n' "$entries" | wc -l) entries)"
  else
    red "    $label: archive is NOT restorable"
    fail=1
    write_manifest "$dir" "$label" "FAILED" "$db"
    return
  fi

  write_manifest "$dir" "$label" "OK" "$db"
  apply_retention "$label" "$RETENTION"
}

backup_mongodb() {
  local ts dir bson dryrun

  ts="$(timestamp)"
  dir="$BACKUP_ROOT/mongodb/$ts"
  mkdir -p "$dir"

  log "mongodb: dumping db=$MONGO_DB from $MONGO_HOST:$MONGO_PORT"
  # No credentials: the order-mongodb Deployment runs without --auth, so there is
  # nothing to authenticate with. If auth is ever enabled, pass the URI via an
  # environment variable built from the Secret - never as a CLI argument, which
  # every other container in the pod can read from `ps`.
  if ! tcp_wait "$MONGO_HOST" "$MONGO_PORT" mongodb; then
    fail=1
    return
  fi

  if ! mongodump --host "$MONGO_HOST" --port "$MONGO_PORT" --db "$MONGO_DB" --out "$dir"; then
    red "    mongodb: mongodump failed"
    fail=1
    return
  fi

  # Verification, in two steps. mongodump's exit status alone is not enough: a
  # dump with no collections produces no files at all (verified against
  # mongo:6.0 - not even an empty directory), so "the command succeeded" and
  # "there is something to restore" are different questions.
  #
  # 1. At least one collection must have been written. A zero-collection dump is
  #    recorded as OK but loudly, because the usual cause is a typo in MONGO_DB,
  #    and a wrong database name would otherwise back up nothing every night.
  bson="$(find "$dir" -name '*.bson' -type f | wc -l)"
  if [ "$bson" -gt 0 ]; then
    # 2. mongorestore --dryRun parses the dump exactly the way a real restore
    #    would and reports what it would do without touching the server. This is
    #    the mongo equivalent of pg_restore --list: it fails on a truncated or
    #    corrupt .bson, which is the failure mode that makes a backup useless.
    #    --nsInclude keeps the dry run inside the namespace that was dumped.
    if dryrun="$(mongorestore --dryRun --nsInclude "$MONGO_DB.*" \
                                --host "$MONGO_HOST" --port "$MONGO_PORT" "$dir" 2>&1)" &&
       printf '%s' "$dryrun" | grep -q 'dry run completed'; then
      green "    mongodb: archive verified ($bson bson files, dry run clean)"
    else
      red "    mongodb: archive is NOT restorable"
      printf '%s\n' "$dryrun" | tail -5 | while IFS= read -r line; do red "      $line"; done
      fail=1
      write_manifest "$dir" mongodb "FAILED" "$MONGO_DB"
      return
    fi
  else
    green "    mongodb: dump OK but empty (0 collections in $MONGO_DB)"
  fi

  write_manifest "$dir" mongodb "OK" "$MONGO_DB"
  apply_retention mongodb "$RETENTION"
}

case "$MODE" in
  postgres)
    require_pg_credentials
    backup_postgres_target auth-postgres    "$AUTH_DB_HOST"    "$AUTH_DB_PORT"    "$AUTH_DB_NAME"    "$AUTH_DB_USER"    "$AUTH_DB_PASSWORD"
    backup_postgres_target catalog-postgres "$CATALOG_DB_HOST" "$CATALOG_DB_PORT" "$CATALOG_DB_NAME" "$CATALOG_DB_USER" "$CATALOG_DB_PASSWORD"
    ;;
  mongodb)
    backup_mongodb
    ;;
  all)
    require_pg_credentials
    backup_postgres_target auth-postgres    "$AUTH_DB_HOST"    "$AUTH_DB_PORT"    "$AUTH_DB_NAME"    "$AUTH_DB_USER"    "$AUTH_DB_PASSWORD"
    backup_postgres_target catalog-postgres "$CATALOG_DB_HOST" "$CATALOG_DB_PORT" "$CATALOG_DB_NAME" "$CATALOG_DB_USER" "$CATALOG_DB_PASSWORD"
    backup_mongodb
    ;;
  *)
    red "unknown mode: $MODE (expected postgres, mongodb or all)"
    exit 2
    ;;
esac

if [ "$fail" -ne 0 ]; then
  red "BACKUP FAILED"
  exit 1
fi
green "BACKUP OK"
