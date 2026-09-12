package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
)

type Product struct {
	ID          int     `json:"id"`
	Name        string  `json:"name"`
	Description string  `json:"description"`
	Price       float64 `json:"price"`
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8082"
	}

	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"UP","service":"catalog-service"}`))
	})

	http.HandleFunc("/products", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		products := []Product{
			{ID: 1, Name: "Смартфон Apple iPhone", Description: "Флагманский телефон из контейнера Go", Price: 999.99},
			{ID: 2, Name: "Наушники AirPods", Description: "Беспроводные наушники", Price: 199.99},
		}
		json.NewEncoder(w).Encode(products)
	})

	log.Printf("=== Clean Go Catalog Service successfully started on port %s ===", port)
	if err := http.ListenAndServe(":"+port, nil); err != nil {
		log.Fatalf("Fatal: Failed to start server: %v", err)
	}
}
