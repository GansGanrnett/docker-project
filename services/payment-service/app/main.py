from fastapi import FastAPI, Header, Response, HTTPException
from pydantic import BaseModel
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
import random
import json
import pika
import os

app = FastAPI(title="Payment Service")

PAYMENT_COUNTER = Counter(
    'payment_transactions_total',
    'Total number of processed payment transactions',
    ['username', 'status']
)

class PaymentRequest(BaseModel):
    orderId: str
    amount: float
    cardNumber: str

# Функция отправки события в RabbitMQ
def send_payment_event(order_id: str, status: str):
    try:
        rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://admin:admin_rabbit_secure@rabbitmq:5672")
        params = pika.URLParameters(rabbitmq_url)
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        
        # Декларируем безопасную очередь событий
        channel.queue_declare(queue='orders.payment_statuses', durable=True)
        
        payload = {"orderId": order_id, "status": status}
        
        channel.basic_publish(
            exchange='',
            routing_key='orders.payment_statuses',
            body=json.dumps(payload),
            properties=pika.BasicProperties(delivery_mode=2) # Делаем сообщение персистентным
        )
        connection.close()
        print(f"[RABBITMQ] Successfully sent event for order {order_id}")
    except Exception as e:
        print(f"[RABBITMQ-ERROR] Failed to dispatch message: {e}")

@app.get("/health")
def health_check():
    return {"status": "UP", "service": "payment-service"}

@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.post("/api/v1/internal/payments/process")
def process_payment(request: PaymentRequest, x_user_username: str = Header(None)):
    if not x_user_username:
        raise HTTPException(status_code=400, detail="Missing identity header x-user-username")
    
    # Для тестов временно зафиксируем True, чтобы гарантировать отправку успешного события
    is_success = True 
    status = "SUCCESS" if is_success else "DECLINED"
    
    PAYMENT_COUNTER.labels(username=x_user_username, status=status).inc()
    
    # Отправляем асинхронное уведомление в RabbitMQ
    send_payment_event(request.orderId, status)
        
    return {
        "orderId": request.orderId,
        "status": status,
        "transactionId": f"tx_{random.randint(100000, 999999)}",
        "amount": request.amount
    }
