import os
import json
import asyncio
from fastapi import FastAPI
import pika
from pika.exceptions import AMQPConnectionError

app = FastAPI(title="Polyglot E-Commerce Analytics Service")

metrics = {
    "total_sales_amount": 0.0,
    "total_orders_count": 0,
    "paid_orders": []
}

# Резервный роут на случай капризов кэша шлюза Express
@app.get("/summary")
@app.get("/api/v1/analytics/summary")
def get_analytics_summary():
    return {
        "status": "HEALTHY",
        "sales_volume_usd": round(metrics["total_sales_amount"], 2),
        "total_processed_transactions": metrics["total_orders_count"],
        "recent_paid_orders": metrics["paid_orders"][-5:]
    }

@app.get("/health")
def health_check():
    return {"status": "UP", "service": "analytics-service"}

def rabbitmq_consumer():
    rabbit_url = os.getenv("RABBITMQ_URL", "amqp://admin:ProdRabbitBrokerPass2026Secure99@rabbitmq:5672")
    
    while True:
        try:
            print(f"[ANALYTICS] Connecting to RabbitMQ at {rabbit_url}...")
            params = pika.URLParameters(rabbit_url)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            
            channel.exchange_declare(exchange='payment.events', exchange_type='topic', durable=True)
            channel.queue_declare(queue='orders.analytics', durable=True)
            channel.queue_bind(exchange='payment.events', queue='orders.analytics', routing_key='payment.success')
            
            def callback(ch, method, properties, body):
                try:
                    event = json.loads(body.decode())
                    order_id = event.get("orderId")
                    amount = float(event.get("amount", 0.0))
                    
                    print(f"📈 [ANALYTICS] Processed payment event for Order ID: {order_id}, Amount: ${amount}")
                    
                    metrics["total_sales_amount"] += amount
                    metrics["total_orders_count"] += 1
                    metrics["paid_orders"].append(order_id)
                    
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                except Exception as e:
                    print(f"[ANALYTICS-ERROR] Error parsing event payload: {e}")
                    ch.basic_ack(delivery_tag=method.delivery_tag)

            channel.basic_consume(queue='orders.analytics', on_message_callback=callback)
            print(" [*] Analytics consumer started successfully. Listening for payment events...")
            channel.start_consuming()
            
        except AMQPConnectionError:
            print("[ANALYTICS-WARN] RabbitMQ is not ready yet. Retrying in 5 seconds...")
            asyncio.run(asyncio.sleep(5))
        except Exception as e:
            print(f"[ANALYTICS-CRIT] Consumer crashed: {e}. Restarting...")
            asyncio.run(asyncio.sleep(5))

import threading
consumer_thread = threading.Thread(target=rabbitmq_consumer, daemon=True)
consumer_thread.start()
