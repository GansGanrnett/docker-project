from fastapi import FastAPI, Header, Response, HTTPException
from pydantic import BaseModel
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
import random

app = FastAPI(title="Payment Service")

# Кастомная метрика Prometheus для отслеживания оплат
PAYMENT_COUNTER = Counter(
    'payment_transactions_total',
    'Total number of processed payment transactions',
    ['username', 'status']
)

class PaymentRequest(BaseModel):
    orderId: str
    amount: float
    cardNumber: str

@app.get("/health")
def health_check():
    return {"status": "UP", "service": "payment-service"}

# Эндпоинт экспорта метрик для Prometheus
@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

# Обработка платежа
@app.post("/api/v1/internal/payments/process")
def process_payment(request: PaymentRequest, x_user_username: str = Header(None)):
    if not x_user_username:
        raise HTTPException(status_code=400, detail="Missing identity header x-user-username")
    
    # Симулируем логику эквайринга (90% транзакций успешны, 10% - отклонены)
    is_success = random.choices([True, False], weights=[90, 10])[0]
    status = "SUCCESS" if is_success else "DECLINED"
    
    # Фиксируем транзакцию в мониторинге
    PAYMENT_COUNTER.labels(username=x_user_username, status=status).inc()
    
    if not is_success:
        return {
            "orderId": request.orderId,
            "status": status,
            "error": "Insufficient funds or bank rejection",
            "amount": request.amount
        }
        
    return {
        "orderId": request.orderId,
        "status": status,
        "transactionId": f"tx_{random.randint(100000, 999999)}",
        "amount": request.amount
    }
