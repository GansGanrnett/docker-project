# ArgoCD — план миграции (см. AGENTS.md, план выше)
# Шаг 1 (обратимый): заменить secrets.yaml на sealed-secrets / external-secrets.
# Сейчас secrets.yaml содержит stringData. Реальные данные только:
# payment-db-password (8fab...), payment-db-url. Остальное — CHANGE_ME.
# Без доступа к кластеру (kubectl соединение отказано) kubeseal не работает;
# SealedSecret не готовим — результат был бы мусор. Миграция в issue #82.
# Действие: после установки kubeseal или external-secrets — создать SealedSecret,
# заменить app-secrets на ссылку в ExternalSecret или удалить stringData.
# Шаг 2: Application CRD (см. argocd/app.yaml) — sync manual сначала, auto после теста.
# Шаг 3: roll back через git revert + argocd app rollback.
