package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"sync/atomic"
	"time"
)

type Product struct {
	ID          int     `json:"id"`
	Name        string  `json:"name"`
	Description string  `json:"description"`
	Price       float64 `json:"price"`
}

var (
	requestsTotal int64
	startTime     = time.Now()
)

func metricsHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintf(w, "# HELP catalog_http_requests_total Total HTTP requests.\n")
	fmt.Fprintf(w, "# TYPE catalog_http_requests_total counter\n")
	fmt.Fprintf(w, "catalog_http_requests_total %d\n", atomic.LoadInt64(&requestsTotal))
	fmt.Fprintf(w, "# HELP catalog_up Is the catalog service up.\n")
	fmt.Fprintf(w, "# TYPE catalog_up gauge\n")
	fmt.Fprintf(w, "catalog_up 1\n")
	fmt.Fprintf(w, "# HELP catalog_uptime_seconds Service uptime.\n")
	fmt.Fprintf(w, "# TYPE catalog_uptime_seconds gauge\n")
	fmt.Fprintf(w, "catalog_uptime_seconds %.0f\n", time.Since(startTime).Seconds())
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8082"
	}

	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt64(&requestsTotal, 1)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"UP","service":"catalog-service"}`))
	})

	http.HandleFunc("/metrics", metricsHandler)

	http.HandleFunc("/products", func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt64(&requestsTotal, 1)
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