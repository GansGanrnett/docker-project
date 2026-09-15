package rabbitmq

import (
	"encoding/json"
	"log"
	"os"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
)

type OrderSagaMessage struct {
	SagaID       string    json:"SagaId"
	OrderID      string    json:"OrderId"
	ProductID    int       json:"ProductId"

Quantity     int       json:"Quantity"
	TotalAmount  float64   json:"TotalAmount"
	CurrentState string    json:"CurrentState"
	Timestamp    time.Time json:"Timestamp"
}

func StartSagaConsumer() {
	rabbitURL := os.Getenv("RABFITMQ_URL")
	if rabbitURL == "" {
		rabbitURL = "amqp://admin:ProdRabbitBrokerPass2026Secure99@message-rabbitmq-service:5672"
	}
	conn, err := amqp.Dial(rabbitURL)
	if err != nil {
		log.Printf("[CATALOG-SAGA] Connection failed: %v", err)
		return
	}
	defer conn.Close()
	ch, err := conn.Channel()
	if err != nil {
		log.Printf("[CATALOG-SAGA] Channel failed: %v", err)
		return
	}
	defer ch.Close()
	_ = ch.ExchangeDeclare("saga.order.events", "topic", true, false, false, false, nil)
	q, _ := ch.QueueDeclare("catalog.order.created", true, false, false, false, nil)
	_ = ch.QueueBind(q.Name, "order.created", "saga.order.events", false, nil)
	msgs, _ := ch.Consume(q.Name, "", false, false, false, false, nil)
	log.Println(" [*] Go Catalog Saga consumer active...")
	for d := range msgs {
		var msg OrderSagaMessage
		_ = json.Unmarshal(d.Body, &msg)
		log.Printf("[AMPP] Received Order: %s", msg.OrderID)
		_ = d.Ack(false)
	}
}
