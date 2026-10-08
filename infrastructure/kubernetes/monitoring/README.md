# Phase 1 — Monitoring setup (observability)

Status: manual deploy (not automated via ArgoCD until rollback test + sealed-secrets done).

## Install kube-prometheus-stack

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f kube-prometheus-stack-values.yaml
```

Token setup (before install):

```bash
kubectl create secret generic alertmanager-telegram-secret \
  -n monitoring --from-literal=TELEGRAM_BOT_TOKEN=<YOUR_TOKEN>
```

## Sync policy reminder
- App `docker-project` stays `manual` (`argocd/app.yaml`).
- Monitoring resources are deployed manually (this folder) until Phase 2 (SLI/SLO + automated deploy review).
