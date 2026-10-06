#!/usr/bin/env bash
#
# preflight-storage.sh - verify that the hostPath volumes bind and are writable.
#
# Run this AFTER `kubectl apply -f base/` on the target cluster:
#
#   ./infrastructure/kubernetes/scripts/preflight-storage.sh
#
# Why this is a check and not a "create the directories" script:
#   storage-volumes.yaml uses `hostPath.type: DirectoryOrCreate`, so kubelet
#   creates the missing directories itself. The only thing left to get wrong is
#   ownership, and the init containers (init-data-dir / init-mongo-data) fix that
#   at pod start. This script proves both facts instead of assuming them.
#
# It is read-only against the cluster: it never creates, patches or deletes a
# resource. Exit code 0 = all bound, non-zero = see the report.
set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
TIMEOUT="${TIMEOUT:-120}"

# PV name -> hostPath directory. Keep in sync with base/storage-volumes.yaml.
declare -A PV_PATHS=(
  [postgres-auth-pv]="/mnt/data/postgres-auth"
  [postgres-catalog-pv]="/mnt/data/postgres-catalog"
  [mongodb-orders-pv]="/mnt/data/mongodb-orders"
  [redis-cart-pv]="/mnt/data/redis"
  [rabbitmq-messages-pv]="/mnt/data/rabbitmq"
  [backups-pv]="/mnt/data/backups"
)

# PVC name -> the PV it is supposed to own. The binder matches a claim to any PV
# with a compatible class/size/accessMode, NOT to the intended one, so a claim
# can bind to the wrong PV and still leave every pod Running and Ready. This
# expected mapping is the only check that catches that mistake.
declare -A EXPECT=(
  [data-auth-postgres-0]=postgres-auth-pv
  [data-catalog-postgres-0]=postgres-catalog-pv
  [mongodb-orders-pvc]=mongodb-orders-pv
  [redis-cart-pvc]=redis-cart-pv
  [rabbitmq-messages-pvc]=rabbitmq-messages-pv
  [backups-pvc]=backups-pv
)

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }

fail=0

echo "==> cluster connectivity"
if ! kubectl cluster-info >/dev/null 2>&1; then
  red "FAIL: cannot reach the cluster. Check your kubeconfig / current-context."
  exit 2
fi
current_ctx="$(kubectl config current-context 2>/dev/null || echo '<none>')"
echo "    context: $current_ctx"
echo "    namespace: $NAMESPACE"

echo
echo "==> storage class 'manual'"
if kubectl get storageclass manual >/dev/null 2>&1; then
  sc="$(kubectl get storageclass manual -o jsonpath='{.provisioner}')"
  vm="$(kubectl get storageclass manual -o jsonpath='{.volumeBindingMode}')"
  if [ "$sc" = "kubernetes.io/no-provisioner" ]; then
    green "    provisioner=$sc (static only), bindingMode=${vm:-Immediate}"
  else
    red "    provisioner=$sc - EXPECTED kubernetes.io/no-provisioner"
    red "      with a real provisioner an unmatched claim gets a dynamic volume on"
    red "      an undeclared path; that is how data lands in the wrong directory"
    fail=1
  fi
else
  yellow "    WARN: storageclass 'manual' missing - the StatefulSet PVCs will stay Pending"
  fail=1
fi

echo
echo "==> persistent volumes"
for pv in "${!PV_PATHS[@]}"; do
  path="${PV_PATHS[$pv]}"
  if ! kubectl get pv "$pv" >/dev/null 2>&1; then
    red "    $pv: NOT FOUND in the cluster"
    fail=1
    continue
  fi

  status="$(kubectl get pv "$pv" -o jsonpath='{.status.phase}')"
  claim="$(kubectl get pv "$pv" -o jsonpath='{.spec.claimRef.name}')"
  [ -z "$claim" ] && claim="<unbound>"

  case "$status" in
    Bound)   green "    $pv: $status -> $claim" ;;
    Pending) yellow "    $pv: $status -> $claim  (expected until a pod is scheduled:
                   volumeBindingMode=WaitForFirstConsumer defers binding)" ;;
    *)       red "    $pv: $status"; fail=1 ;;
  esac
  echo "      hostPath: $path"
done

echo
echo "==> claims bind to the PV they were designed for"
# The StatefulSet claims (data-auth-postgres-0 / data-catalog-postgres-0) only
# exist once their StatefulSets have been created, so a missing claim is a
# warning, not a failure.
for claim in "${!EXPECT[@]}"; do
  want="${EXPECT[$claim]}"
  if ! kubectl -n "$NAMESPACE" get pvc "$claim" >/dev/null 2>&1; then
    yellow "    $claim: not created yet (expected $want)"
    continue
  fi

  phase="$(kubectl -n "$NAMESPACE" get pvc "$claim" -o jsonpath='{.status.phase}')"
  got="$(kubectl -n "$NAMESPACE" get pvc "$claim" -o jsonpath='{.spec.volumeName}')"

  case "$phase" in
    Bound)
      if [ "$got" = "$want" ]; then
        green "    $claim -> $got"
      else
        red "    $claim -> $got  EXPECTED $want"
        red "      wrong volume: bound to a different PV of the same class"
        fail=1
      fi
      ;;
    Pending)
      yellow "    $claim: Pending (expected $want; WaitForFirstConsumer defers binding until a pod is scheduled)"
      ;;
    *)
      red "    $claim: $phase"; fail=1 ;;
  esac
done

echo
echo "==> pod ownership of the data directory"
# Confirms the init containers actually ran and chowned the mount.
#
# The reason is read from `state.terminated.reason`, NOT from `state`.
# `state` alone renders as a Go map
# (map[terminated:map[exitCode:0 reason:Completed ...]]), so comparing it to
# the string "terminated" is never true: the branch below silently never ran
# and the ownership stat - the entire point of this check - was never made.
check_init() {
  local sel="$1" cname="$2" container="$3" path="$4"

  # `kubectl get pod -l` SUCCEEDS with an empty list when the workload is
  # scaled to zero, so its exit status cannot be used as an existence check.
  # The pod name has to be extracted explicitly, and an empty result means
  # "no pod to inspect" rather than an error - otherwise `{.items[0]...}`
  # fails with "array index out of bounds" and, under `set -e`, kills the
  # whole script.
  local pod
  pod="$(kubectl -n "$NAMESPACE" get pod -l "app=$sel" \
           -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [ -z "$pod" ]; then
    yellow "    $sel: no pod (scaled to zero, or not scheduled yet)"
    return
  fi

  local reason
  # `|| true` is load-bearing: under `set -e` a failing command substitution in
  # an assignment aborts the whole script. An unreadable state must degrade to
  # a warning, not a crash.
  reason="$(kubectl -n "$NAMESPACE" get pod "$pod" \
              -o jsonpath="{.status.initContainerStatuses[?(@.name==\"$cname\")].state.terminated.reason}" 2>/dev/null || true)"
  if [ "$reason" != "Completed" ]; then
    yellow "    $cname on $pod: ${reason:-not completed yet}"
    return
  fi
  green "    $cname on $pod: completed"

  local owner
  owner="$(kubectl -n "$NAMESPACE" exec "$pod" -c "$container" \
             -- stat -c '%u:%g %a' "$path" 2>/dev/null || echo '?')"
  case "$owner" in
    999:999*) green "    $path owned by $owner" ;;
    *)        yellow "    WARN: $path owned by $owner (expected 999:999)" ;;
  esac
}

# Every hostPath-backed workload chowns its mount to uid 999, so every one of
# them is verified - a PV that binds is not evidence that the service can write
# to it.
check_init auth-postgres    init-data-dir     postgres         /var/lib/postgresql/data
check_init catalog-postgres init-data-dir     postgres         /var/lib/postgresql/data
check_init order-mongodb    init-mongo-data   order-mongodb    /data/db
check_init cart-redis       init-redis-data   cart-redis       /data
check_init message-rabbitmq init-rabbitmq-data message-rabbitmq /var/lib/rabbitmq

# The backups PVC is different from the five above: its writer is a CronJob, and
# Job pods are deleted once they finish, so there is no long-lived pod to stat and
# check_init would only ever report "no pod". What matters there is that the
# chown survived into the Job templates. If init-backups-data disappears, a PVC
# recreated with a fresh root-owned /backups makes every backup fail - and the
# first scheduled run after a node reboot is where that shows up.
for cj in backup-postgres backup-mongodb; do
  init_name="$(kubectl -n "$NAMESPACE" get cronjob "$cj" \
    -o jsonpath='{.spec.jobTemplate.spec.template.spec.initContainers[0].name}' 2>/dev/null || true)"
  case "$init_name" in
    init-backups-data) green "    $cj: init-backups-data present (chowns /backups to 999:999)" ;;
    *) red "    $cj: expected init-backups-data, found '${init_name:-none}' - backups PVC would stay root-owned"; fail=1 ;;
  esac
done

echo
if [ "$fail" -eq 0 ]; then
  green "PREFLIGHT OK - volumes bound and owned by the service user"
  exit 0
fi
red "PREFLIGHT FAILED - see the red lines above"
exit 1
