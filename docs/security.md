# Security notes

## Инцидент: пароль payment-db в git (октябрь 2026)

Пароль `payment-db-password` был закоммичен в
`infrastructure/kubernetes/base/secrets.yaml` в коммитах:

- `c841b74` — feat(infra): run migrations, wire db secret and CI postgres (F-22)
- `c1e774e` — feat(infra): add payment database for idempotent payments (F-22)

Значение также упоминалось в `infrastructure/kubernetes/argocd/README.md`.

**Что сделано:**

- Значение в main заменено на `CHANGE_ME` (как остальные секреты в этом файле);
- Упоминание убрано из ArgoCD README;
- CI-grep расширен на `.go`/`.sh`/все текстовые файлы (`exclude-dir=.git/node_modules/bin/obj`), добавлен `postgresql://`;

**Что НЕ сделано (осознанно):**

- История НЕ переписана (`git filter-repo` + force-push не применялись).
  Причина: репозиторий публичный, есть форки и кэши GitHub; перезапись
  истории сломает их и не даёт гарантии, что копия не осталась в другом
  месте.
- Пароль не ротирован (кластер с этим значением не разворачивался, значение
  не использовалось нигде за пределами тестового kind).

**Действия для будущего продукта:**

Если проект переходит в продукт или кластер будет развёрнут с этим значением:

1. Ротировать пароль в `payment-postgres`.
2. Провести `git filter-repo --replace-text` с картой `8fab…==>***REMOVED***`.
3. Force-push в main (предупредить контрибьюторов, форки перестанут
   мержиться).
4. Перейти на SealedSecrets или External Secrets Operator (issue #82).

**Ссылки:**
- security audit 2026-10-13
- S1: security audit