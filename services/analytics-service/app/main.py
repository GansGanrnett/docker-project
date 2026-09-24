import os
import json
import threading
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response
import pika
from pika.exceptions import AMQPConnectionError
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST


@asynccontextmanager
async def lifespan(app):
    # Фоновый консьюмер запускается через lifespan, а не на этапе импорта:
    # импорт модуля (в т.ч. тестами) не имеет побочных эффектов.
    consumer_thread = threading.Thread(target=rabbitmq_consumer, daemon=True)
    consumer_thread.start()
    yield
    # При завершении приложения поток daemon завершится вместе с процессом.


app = FastAPI(title="Polyglot E-Commerce Analytics Service", lifespan=lifespan)

# Метрики Prometheus
SUMMARY_REQUESTS = Counter("analytics_summary_requests_total", "Total summary requests")
PAYMENT_EVENTS = Counter("analytics_payment_events_total", "Total processed payment events")

PAYMENT_EXCHANGE = "payment.events"
DEAD_LETTER_EXCHANGE = "payment.events.dlx"
DEAD_LETTER_QUEUE = "orders.analytics.dlq"

# Агрегаты аналитики: словарь называется aggregates, а не metrics,
# чтобы не конфликтовать с функцией-эндпоинтом metrics().
aggregates = {
    "total_sales_amount": 0.0,
    "total_orders_count": 0,
    "paid_orders": []
}

aggregates_lock = threading.Lock()


@app.get("/summary")
@app.get("/api/v1/analytics/summary")
def get_analytics_summary():
    SUMMARY_REQUESTS.inc()
    with aggregates_lock:
        return {
            "status": "HEALTHY",
            "sales_volume_usd": round(aggregates["total_sales_amount"], 2),
            "total_processed_transactions": aggregates["total_orders_count"],
            "recent_paid_orders": aggregates["paid_orders"][-5:]
        }


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health_check():
    return {"status": "UP", "service": "analytics-service"}


def rabbitmq_consumer():
    rabbit_url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672")

    while True:
        try:
            print(f"[ANALYTICS] Connecting to RabbitMQ at {rabbit_url}...")
            params = pika.URLParameters(rabbit_url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()

            # Единый topic-exchange с payment-service: payment.main публикует
            # с routing key payment.success/payment.declined в exchange payment.events
            channel.exchange_declare(
                exchange=PAYMENT_EXCHANGE, exchange_type='topic', durable=True)
            channel.exchange_declare(
                exchange=DEAD_LETTER_EXCHANGE, exchange_type='fanout', durable=True)
            channel.queue_declare(queue=DEAD_LETTER_QUEUE, durable=True)
            channel.queue_bind(exchange=DEAD_LETTER_EXCHANGE, queue=DEAD_LETTER_QUEUE)

            queue = channel.queue_declare(
                queue='orders.analytics', durable=True,
                arguments={"x-dead-letter-exchange": DEAD_LETTER_EXCHANGE})
            channel.queue_bind(exchange=PAYMENT_EXCHANGE, queue='orders.analytics',
                               routing_key='payment.success')

            def callback(ch, method, properties, body):
                try:
                    event = json.loads(body.decode())
                    order_id = event.get("orderId")
                    amount = float(event.get("amount", 0.0))

                    print(f"[ANALYTICS] Processed payment event for Order ID: {order_id}, Amount: ${amount}")

                    with aggregates_lock:
                        aggregates["total_sales_amount"] += amount
                        aggregates["total_orders_count"] += 1
                        aggregates["paid_orders"].append(order_id)
                    PAYMENT_EVENTS.inc()

                    # Ack ТОЛЬКО после успешной обработки
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                except Exception as e:
                    print(f"[ANALYTICS-ERROR] Error parsing event payload: {e}")
                    # Битое сообщение -> в DLQ, не теряем и не зацикливаем requeue
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

            channel.basic_consume(queue='orders.analytics', on_message_callback=callback)
            print(" [*] Analytics consumer started successfully. Listening for payment events...")
            channel.start_consuming()

        except AMQPConnectionError:
            print("[ANALYTICS-WARN] RabbitMQ is not ready yet. Retrying in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            print(f"[ANALYTICS-CRIT] Consumer crashed: {e}. Restarting...")
            time.sleep(5)