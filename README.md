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
