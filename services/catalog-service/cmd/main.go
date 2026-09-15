package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"project-root/services/catalog-service/pkg/rabbitmq"
)

type correlationKey string
const correlationIDKey correlationKey = "correlationId"

type Product struct {
	ID          int     `json:"id"`
	Name        string  `json:"name"`
	Price       float64 `json:"price"`
	Description string  `json:"description"`
}

func tracingMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		correlationID := r.Header.Get("X-Correlation-ID")
		if correlationID == "" {
			correlationID = "no-id"
		}
		ctx := context.WithValue(r.Context(), correlationIDKey, correlationID)
		w.Header().Set("X-Correlation-ID", correlationID)
		next.ServeHTTP(w, r.WithContext(ctx))
	}
}

func getProductsHandler(w http.ResponseWriter, r *http.Request) {
	ctxID := r.Context().Value(correlationIDKey).(string)
	log.Printf("[%s] [CATALOG] Processing fetch products request", ctxID)
	w.Header().Set("Content-Type", "application/json")
	products := []Product{
		{ID: 1, Name: "Смартфон Apple iPhone", Price: 999.99, Description: "Флагман контура"},
	}
	json.NewEncoder(w).Encode(products)
}

func main() {
	log.Println("=== Запуск Catalog Service на Go с поддержкой Saga ===")
	go rabbitmq.StartSagaConsumer()
	http.HandleFunc("/products", tracingMiddleware(getProductsHandler))
	log.Fatal(http.ListenAndServe(":8082", nil))
}
