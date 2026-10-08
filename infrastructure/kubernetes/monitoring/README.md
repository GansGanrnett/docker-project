# Phase 1 — Monitoring setup (observability)

Status: manual deploy (not automated via ArgoCD until rollback test + sealed-secrets done).

## Install kube-prometheus-stack

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f kube-prometheus-stack-values.yaml
```

## Secret setup — bot token only (do NOT commit token to git)

Create `telegram-token.txt` locally (do NOT add to git):

```bash
echo "<TOKEN>" > telegram-token.txt
chmod 600 telegram-token.txt
```

Create Kubernetes secret in `monitoring` namespace:

```bash
kubectl create secret generic alertmanager-telegram-secret \
  -n monitoring \
  --from-file=bot_token=./telegram-token.txt
```

Get `chat_id` by sending a message to the bot and calling `getUpdates` (replace `<TOKEN>`):

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep 'chat'
```

Use the returned `chat_id` (e.g., replace `<CHAT_ID>` in values.yaml or pass via `--set`).

## Deploy with chat_id

Replace `<CHAT_ID>` in `kube-prometheus-stack-values.yaml` or pass at install:

```bash
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f kube-prometheus-stack-values.yaml \
  --set alertmanager.config.receivers[0].telegram_configs[0].chat_id=<CHAT_ID>
```

Note: `chat_id` is set directly in values.yaml (placeholder `<CHAT_ID>`); bot token stays only in the Kubernetes secret (`alertmanager-telegram-secret`).

## How to verify alerts are delivered

Port-forward Alertmanager locally:

```bash
kubectl port-forward svc/alertmanager 9093:9093 -n monitoring
```

Send a test alert with `amtool`:

```bash
amtool --alertmanager.url=http://localhost:9093 alert add test-alert alertname=test severity=warning
```

Check Telegram bot chat history for the message.

## Sync

`app.yaml` uses `manual` sync (`RespectIgnoreDifferences: true`) — no automated ArgoCD deploy until sealed-secrets + rollback test completed.

## Placeholder values (local only — NOT in repo)

- bot_token: `<TOKEN>` (file `telegram-token.txt` only)
- chat_id: `<CHAT_ID>` (substitute before deploy; never commit real value)
