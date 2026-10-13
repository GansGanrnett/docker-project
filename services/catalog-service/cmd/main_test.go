package main

import (
	"testing"

	amqp "github.com/rabbitmq/amqp091-go"
)

func TestProcessMessage_Success(t *testing.T) {
	msg := amqp.Delivery{Body: []byte(`{"order_id":42,"action":"compensate"}`), MessageId: "m-1"}
	if !processMessage(msg) {
		t.Error("expected success for valid message")
	}
}
