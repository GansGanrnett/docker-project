package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
)

type correlationKey string
const correlationIDKey correlationKey = "correlationId"

type Product struct {
	ID          int     `json:"id"`
	Name        string  `json:"name"`
	Price       float64 `json:"price"`
	Description string  `json:"description"`
}

// Перехватчик трассировки для извлечения заголовка X-Correlation-ID
func tracingMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		correlationID := r.Header.Get("X-Correlation-ID")
		if correlationID == "" {
			correlationID = "no-id"
		}

		// Помещаем идентификатор в контекст выполнения запроса
		ctx := context.WithValue(r.Context(), correlationIDKey, correlationID)
		
		// Дублируем заголовок в HTTP-ответ для сквозного контроля
		w.Header().Set("X-Correlation-ID", correlationID)
		
		next.ServeHTTP(w, r.WithContext(ctx))
	}
}

func getProductsHandler(w http.ResponseWriter, r *http.Request) {
	// Извлекаем идентификатор из контекста для логирования операции
	ctxID := r.Context().Value(correlationIDKey).(string)
	log.Printf("[%s] [CATALOG] Processing fetch products request", ctxID)

	w.Header().Set("Content-Type", "application/json")
	products := []Product{
		{ID: 1, Name: "Смартфон Apple iPhone", Price: 999.99, Description: "Флагманский смартфон контура Elysium"},
		{ID: 2, Name: "Ноутбук ASUS ROG", Price: 1999.99, Description: "Игровая рабочая станция бэкенда"},
	}
	json.NewEncoder(w).Encode(products)
}

func main() {
	log.Println("=== Запуск Catalog Service на Go с поддержкой Трассировки ===")

	http.HandleFunc("/products", tracingMiddleware(getProductsHandler))
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"UP","service":"catalog-service"}`))
	})

	log.Fatal(http.ListenAndServe(":8082", nil))
}
