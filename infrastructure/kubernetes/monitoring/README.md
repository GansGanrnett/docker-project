# Phase 1 — Monitoring setup (observability)

Status: manual deploy (not automated via ArgoCD until rollback test + sealed-secrets done).

## Install kube-prometheus-stack

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f kube-prometheus-stack-values.yaml
```

## Secret setup — bot token only

Create the secret with bot_token file (do NOT put chat_id in secret — set directly in values.yaml as `<CHAT_ID>` or replace after install):

```bash
kubectl create secret generic alertmanager-telegram-secret \
  -n monitoring \
  --from-file=bot_token=./telegram-token.txt
```

Replace `<CHAT_ID>` in `kube-prometheus-stack-values.yaml` before install (or via `helm upgrade --set` after).
