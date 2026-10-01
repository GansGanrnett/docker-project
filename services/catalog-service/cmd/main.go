package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

type Product struct {
	ID          int     `json:"id"`
	Name        string  `json:"name"`
	Description string  `json:"description"`
	Price       float64 `json:"price"`
}

var startTime = time.Now()

func init() {
	// promhttp.Handler() already exposes the default Go collector and the
	// process collector (go_*, process_*), so only service-level gauges are
	// added here.
	prometheus.MustRegister(
		prometheus.NewGaugeFunc(prometheus.GaugeOpts{
			Name: "catalog_up",
			Help: "Is the catalog service up.",
		}, func() float64 { return 1 }),
		prometheus.NewGaugeFunc(prometheus.GaugeOpts{
			Name: "catalog_uptime_seconds",
			Help: "Service uptime.",
		}, func() float64 { return time.Since(startTime).Seconds() }),
	)
}

func newMux() *http.ServeMux {
	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"UP","service":"catalog-service"}`))
	})

	mux.HandleFunc("/products", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		products := []Product{
			{ID: 1, Name: "Смартфон Apple iPhone", Description: "Флагманский телефон из контейнера Go", Price: 999.99},
			{ID: 2, Name: "Наушники AirPods", Description: "Беспроводные наушники", Price: 199.99},
		}
		json.NewEncoder(w).Encode(products)
	})

	return mux
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8082"
	}

	// Real latency + status-code metrics, replacing the hardcoded stub that
	// always reported catalog_http_requests_total 0 / catalog_up 1.
	// Label names are fixed by promhttp: only "code" and "method" are allowed.
	inFlight := prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "http_requests_in_progress",
		Help: "Current number of HTTP requests being served.",
	})
	requestsTotal := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "http_requests_total",
		Help: "Total number of HTTP requests processed.",
	}, []string{"code", "method"})
	requestDuration := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "http_request_duration_seconds",
		Help:    "HTTP request latencies in seconds.",
		Buckets: prometheus.DefBuckets,
	}, []string{"code", "method"})
	prometheus.MustRegister(inFlight, requestsTotal, requestDuration)

	instrumented := promhttp.InstrumentHandlerDuration(
		requestDuration,
		promhttp.InstrumentHandlerCounter(
			requestsTotal,
			promhttp.InstrumentHandlerInFlight(inFlight, newMux()),
		),
	)

	// /metrics is mounted outside the instrumented mux so that a scrape does
	// not inflate the very counters and histograms it is about to report.
	root := http.NewServeMux()
	root.Handle("/metrics", promhttp.Handler())
	root.Handle("/", instrumented)

	log.Printf("=== Clean Go Catalog Service successfully started on port %s ===", port)
	if err := http.ListenAndServe(":"+port, root); err != nil {
		log.Fatalf("Fatal: Failed to start server: %v", err)
	}
}
