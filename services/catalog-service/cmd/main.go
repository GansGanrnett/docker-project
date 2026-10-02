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

// healthHandler is the liveness probe: the process is up and the listener
// accepts connections. It must not touch anything external - a liveness probe
// that fails during a dependency outage makes kubelet restart every replica,
// which does not fix the dependency and turns an outage into an outage plus a
// crash loop.
func healthHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"UP","service":"catalog-service"}`))
	}
}

// readyHandler is the readiness probe. catalog has no external dependencies -
// it serves a hardcoded product list - so readiness is the same check as
// liveness. It is a separate function so that adding a database later changes
// this one place and leaves the liveness contract alone.
func readyHandler() http.HandlerFunc {
	return healthHandler()
}

// newMux holds the business routes only. Probes and metrics are mounted by
// newRootHandler, outside the instrumented wrapper.
func newMux() *http.ServeMux {
	mux := http.NewServeMux()

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

// newRootHandler wires the whole HTTP surface. Probes and /metrics sit outside
// the instrumented business mux: kubelet polls /health and /ready every
// periodSeconds, and counting that traffic would leave http_requests_total
// mostly probe noise, with /products as a rounding error. The registry and
// gatherer are parameters so tests can build an isolated set of metrics.
func newRootHandler(registerer prometheus.Registerer, gatherer prometheus.Gatherer) http.Handler {
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
	registerer.MustRegister(inFlight, requestsTotal, requestDuration)

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
	root.Handle("/metrics", promhttp.HandlerFor(gatherer, promhttp.HandlerOpts{}))
	root.Handle("/health", healthHandler())
	root.Handle("/ready", readyHandler())
	root.Handle("/", instrumented)

	return root
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8082"
	}

	handler := newRootHandler(prometheus.DefaultRegisterer, prometheus.DefaultGatherer)

	log.Printf("=== Clean Go Catalog Service successfully started on port %s ===", port)
	if err := http.ListenAndServe(":"+port, handler); err != nil {
		log.Fatalf("Fatal: Failed to start server: %v", err)
	}
}
