# Security Audit Findings — docker-project

Дата аудита: 2026-09-30
Ветка: `audit-fixes-2` (база `origin/project`)

## Finding 1: pg_hba.conf trust для localhost

**Серьёзность:** high

**Источник:** поведение наблюдалось в работающем контейнере `postgres:15-alpine`.
В репозитории файла `pg_hba.conf` нет — это дефолт официального образа.
Проблема актуальна для боевого сервера, где контейнеры запускаются с тем же образом.

**Где:** контейнеры Postgres (`auth-postgres`, `catalog-postgres`), файл
`/var/lib/postgresql/data/pg_hba.conf`

**Суть:** в `pg_hba.conf` для `local` и `127.0.0.1/32` стоит `trust`. Postgres читает
правила сверху вниз, первое совпадение выигрывает. Подключение с localhost попадает
в правило `trust` и до `scram-sha-256` не доходит.

**Последствие:** любой процесс внутри контейнера Postgres, включая `docker exec`,
получает полный доступ к БД без пароля. Смена пароля защищает только TCP-подключения
извне docker-сети.

**Воспроизведение:** в песочнице
`docker exec auth-postgres psql -U test_user -d auth_db -c "SELECT 1"` проходит без пароля.

**Решение для боевого сервера:**
1. Добавить в compose volume с кастомным `pg_hba.conf` или использовать
   `POSTGRES_INITDB_ARGS` для смены метода аутентификации.
2. Заменить `trust` на `scram-sha-256` для `local` и `127.0.0.1/32`.
3. Перезапустить Postgres.
4. Проверить, что `auth-service` и `catalog-service` подключаются с паролем —
   убедиться, что `AUTH_DB_PASSWORD` и `CATALOG_DB_PASSWORD` реально доходят
   до сервисов через env.

## Finding 2: Hardcoded fallback ***REMOVED*** в RabbitMQ

**Серьёзность:** high

**Где:**
- `services/analytics-service/app/main.py:67`
- `services/payment-service/app/main.py:56`

**Суть:** в коде был fallback `amqp://***REMOVED***@rabbitmq:5672`. Если env
`RABBITMQ_URL` не задан, сервис молча подключался бы дефолтной учёткой.

**Статус:** исправлено в этой ветке. Fallback удалён, при отсутствии env сервис
падает с ошибкой (fail-fast).

**Осталось:** почистить git-историю — отдельная задача, требует предварительной
смены паролей на боевом сервере.

## Finding 3: Несогласованное экранирование в healthcheck Postgres

**Серьёзность:** medium

**Где:** `docker-compose.yml`, сервисы `auth-db` и `catalog-db`.

**Суть:**
- `auth-db`: `pg_isready -U $${AUTH_DB_USER} -d $${AUTH_DB_NAME}` — двойной `$$`
  экранирует подстановку Compose, но внутри контейнера переменные `AUTH_DB_USER`
  и `AUTH_DB_NAME` не заданы (там только `POSTGRES_USER` / `POSTGRES_DB`).
  Healthcheck получает пустые значения.
- `catalog-db`: `pg_isready -U ${CATALOG_DB_USER} -d ${CATALOG_DB_NAME}` —
  одинарный `${}`, Compose подставляет значения из `.env` при парсинге.
  Работает корректно.

**Последствие:** healthcheck для `auth-db` может ложно срабатывать или всегда падать
в зависимости от поведения `pg_isready` при пустых аргументах. Это создаёт ложное
впечатление, что сервис здоров, и может маскировать реальные проблемы с аутентификацией.

**Решение:** привести к одному стилю — одинарный `${}`, Compose подставит значения
из `.env`:

```yaml
test: ["CMD-SHELL", "pg_isready -U ${AUTH_DB_USER} -d ${AUTH_DB_NAME}"]
```

## Ограничения текущей проверки

Контейнерный прогон не выполнялся: на машине не починен Docker
(`exec /bin/sh: input/output error` на первом `RUN`-шаге сборки). Проверка правок
выполнена статически: чтение файлов и `python -m py_compile` для обоих сервисов.
Finding 1 подтверждён в песочнице до начала работ, в текущей сессии не перепроверялся.
