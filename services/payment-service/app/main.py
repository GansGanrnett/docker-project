import os
import json
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import pika

app = FastAPI(title="Elysium Payment Service with Tracing")

class PaymentRequest(BaseModel):
    orderId: str
    amount: float
    cardNumber: str

@app.post("/api/v1/internal/payments/process")
async def process_payment(payload: PaymentRequest, request: Request):
    # Извлекаем ID трассировки, проброшенный шлюзом через HTTP заголовок
    correlation_id = request.headers.get("X-Correlation-ID", "no-id")
    print(f"[{correlation_id}] [PAYMENT] Received transaction request for order {payload.orderId}")
    
    event_payload = {
        "orderId": payload.orderId,
        "status": "SUCCESS",
        "transactionId": f"tx_{os.getpid()}",
        "amount": payload.amount
    }
    
    rabbit_url = os.getenv("RABBITMQ_URL", "amqp://admin:ProdRabbitBrokerPass2026Secure99@rabbitmq:5672")
    try:
        params = pika.URLParameters(rabbit_url)
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        channel.exchange_declare(exchange='payment.events', exchange_type='topic', durable=True)
        
        # ПРОБРОС В ШИНУ: Впекаем заголовок трассировки в метаданные сообщения RabbitMQ
        properties = pika.BasicProperties(
            delivery_mode=2,
            headers={"X-Correlation-ID": correlation_id}
        )
        
        channel.basic_publish(
            exchange='payment.events',
            routing_key='payment.success',
            body=json.dumps(event_payload),
            properties=properties
        )
        connection.close()
        print(f"[{correlation_id}] [PAYMENT-AMQP] Emitted payment.success event into transport layer")
    except Exception as e:
        print(f"[{correlation_id}] [PAYMENT-ERROR] Broker injection crash: {e}")
        raise HTTPException(status_code=500, detail="Ошибка транспортной шины сообщений")

    return event_payload

@app.get("/health")
def health_check():
    return {"status": "UP", "service": "payment-service"}
