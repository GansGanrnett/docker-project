#  Polyglot Microservices Cluster with CI/CD & Monitoring

[![CI/CD](https://github.com/GansGanrnett/docker-project/actions/workflows/ci.yml/badge.svg)](https://github.com/GansGanrnett/docker-project/actions)
[![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![Prometheus](https://img.shields.io/badge/Prometheus-E6522C?logo=prometheus&logoColor=white)](https://prometheus.io/)
[![Grafana](https://img.shields.io/badge/Grafana-F46800?logo=grafana&logoColor=white)](https://grafana.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Полиглотный микросервисный кластер с автоматическим CI/CD, мониторингом и RSA-шифрованием. Проект демонстрирует навыки контейнеризации, оркестрации, автоматизации развёртывания и observability.

##  Архитектура

Проект состоит из следующих микросервисов, каждый из которых упакован в отдельный Docker-контейнер:

- **API Gateway** — единая точка входа, маршрутизация запросов.
- **Auth Service** — аутентификация и авторизация (JWT, RSA-подписи).
- **Catalog Service** — управление каталогом товаров.
- **Cart Service** — корзина покупок.
- **Order Service** — оформление и обработка заказов.
- **Payment Service** — эмуляция платёжного шлюза.
- **Analytics Service** — сбор и агрегация статистики.

```text
Client → Nginx → API Gateway → [Auth, Catalog, Cart, Order, Payment, Analytics] → PostgreSQL
                                        │
                                        ├── RabbitMQ  (order ⇄ payment/analytics, события заказов)
                                        ├── MongoDB   (заказы, корзина)
                                        └── Redis     (корзина, кэш)
```

Полное описание взаимодействий и контрактов — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

##  Быстрый старт

### 1. Конфигурация

```bash
cp .env.example .env
# Заполнить пароли: .env нельзя оставлять с плейсхолдерами вида <сгенерируйте-...>
```

### 2. Ключи JWT

Auth-service подписывает токены парой RSA-4096, которая **не хранится в репозитории**:
`private.pem` попадает под `*.pem` в `.gitignore`, а в образ ключи не копируются.
Без этого шага контейнер auth-service падает при старте.

```bash
scripts/generate-jwt-keys.sh
```

Скрипт сгенерирует `services/auth-service/configs/{private,public}.pem` и скопирует
`public.pem` в каталоги API Gateway и Order Service — compose монтирует ключи именно
оттуда. Приватный ключ остаётся только в `services/auth-service/configs` и в образ
не попадает.

### 3. Запуск

```bash
make up      # docker compose up -d --build
make logs    # поды на месте, но ключи могли не смонтироваться - смотрим логи
make down
```

##  Ключи JWT

Пара ключей генерируется ровно один раз и дальше только проверяется. Это не
осторожность, а требование: **смена ключа инвалидирует все выпущенные токены**,
и при `replicaCount > 1` реплики, получившие разные пары, перестают доверять
друг другу — токен, подписанный одной, не проходит проверку на другой.

| Среда | Как появляются ключи |
| --- | --- |
| Docker Compose | `scripts/generate-jwt-keys.sh` вручную, монтирование в `docker-compose.yml` |
| Kubernetes | hook-job чарта создаёт Secret `auth-jwt-keys` при первом развёртывании |
| CI | job `Verify JWT key bootstrap` проверяет генератор, идемпотентность и отсутствие `private.pem` в индексе git |

### Проверка и генерация

```bash
scripts/generate-jwt-keys.sh                        # сгенерировать, если ключей нет
scripts/generate-jwt-keys.sh --check                # проверить, ничего не меняя
scripts/generate-jwt-keys.sh --check /path/to/dir   # то же для произвольного каталога
```

Проверка сверяет не только существование файлов, но и то, что они совместимы с кодом:
заголовки `BEGIN PRIVATE KEY` (PKCS#8) и `BEGIN PUBLIC KEY` (X.509 SPKI) — ровно те,
которые разбирает `JwtConfig`, а модули приватного и публичного ключа равны.
Пара, у которой совпали имена, но разошлись ключи, отвергается.

### Ротация

Автоматической ротации нет намеренно. Если ключ нужно сменить:

```bash
# Ожидаемое окно простоя: все ранее выданные токены перестанут работать.
kubectl delete secret auth-jwt-keys -n <release-ns>
helm upgrade auth infrastructure/kubernetes/helm-charts/auth-service
```

Локально — удалите `services/auth-service/configs/private.pem` и `public.pem`,
после чего запустите генератор заново.

##  Развёртывание в Kubernetes

```bash
helm install auth  infrastructure/kubernetes/helm-charts/auth-service
helm install order infrastructure/kubernetes/helm-charts/order-service
```

Установка чарта auth-service выполняет hook-job, который создаёт Secret с ключами
(если его ещё нет) и монтирует пару в `/app/configs`. Существующий Secret hook
никогда не перезаписывает — см. `jwt.keygen` в
[values.yaml](infrastructure/kubernetes/helm-charts/auth-service/values.yaml).

Если ключи приходят из внешнего хранилища (Vault, External Secrets Operator),
поставьте `jwt.keygen.enabled=false` и создайте Secret `auth-jwt-keys` заранее.