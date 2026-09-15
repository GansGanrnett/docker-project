import os
import json
import asyncio
from fastapi import FastAPI
import pika
from pika.exceptions import AMQPConnectionError

app = FastAPI(title="Elysium Analytics Service with Tracing")

metrics = {
    "total_sales_amount": 0.0,
    "total_orders_count": 0,
    "paid_orders": []
}

@app.get("/summary")
def get_analytics_summary():
    return {
        "status": "HEALTHY",
        "sales_volume_usd": round(metrics["total_sales_amount"], 2),
        "total_processed_transactions": metrics["total_orders_count"]
    }

def rabbitmq_consumer():
    rabbit_url = os.getenv("RABBITMQ_URL", "amqp://admin:ProdRabbitBrokerPass2026Secure99@message-rabbitmq-service:5672")
    
    while True:
        try:
            params = pika.URLParameters(rabbit_url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.exchange_declare(exchange='payment.events', exchange_type='topic', durable=True)
            channel.queue_declare(queue='orders.analytics', durable=True)
            channel.queue_bind(exchange='payment.events', queue='orders.analytics', routing_key='payment.success')
            
            def callback(ch, method, properties, body):
                correlation_id = "no-id"
                if properties.headers and "X-Correlation-ID" in properties.headers:
                    correlation_id = properties.headers["X-Correlation-ID"]

                try:
                    event = json.loads(body.decode())
                    order_id = event.get("orderId")
                    amount = float(event.get("amount", 0.0))
                    
                    print(f"[{correlation_id}] [ANALYTICS] Accumulated metric increment for order {order_id}: +${amount}")
                    
                    metrics["total_sales_amount"] += amount
                    metrics["total_orders_count"] += 1
                    metrics["paid_orders"].append(order_id)
                    
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                except Exception as e:
                    print(f"[{correlation_id}] [ANALYTICS-ERROR] Operational failure: {e}")
                    ch.basic_ack(delivery_tag=method.delivery_tag)

            channel.basic_consume(queue='orders.analytics', on_message_callback=callback)
            print(" [*] Analytics consumer loop active. Awaiting correlated events...")
            channel.start_consuming()
            
        except AMQPConnectionError:
            asyncio.run(asyncio.sleep(5))
        except Exception:
            asyncio.run(asyncio.sleep(5))

import threading
threading.Thread(target=rabbitmq_consumer, daemon=True).start()
