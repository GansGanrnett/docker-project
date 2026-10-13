package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	amqp "github.com/rabbitmq/amqp091-go"
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

func startConsumer(ctx context.Context) {
	amqpURL := os.Getenv("AMQP_URL")
	if amqpURL == "" {
		amqpURL = "amqp://guest:guest@localhost:5672/"
	}
	queue := os.Getenv("CATALOG_QUEUE")
	if queue == "" {
		queue = "catalog.saga"
	}

	conn, err := amqp.Dial(amqpURL)
	if err != nil {
		log.Printf("[catalog-consumer] connection failed: %v", err)
		return
	}
	defer conn.Close()

	ch, err := conn.Channel()
	if err != nil {
		log.Printf("[catalog-consumer] channel failed: %v", err)
		return
	}
	defer ch.Close()

	// prefetch = 1 (как analytics F-33)
	if err := ch.Qos(1, 0, false); err != nil {
		log.Printf("[catalog-consumer] qos failed: %v", err)
		return
	}

	_, err = ch.QueueDeclare(queue, true, false, false, false, nil)
	if err != nil {
		log.Printf("[catalog-consumer] queue declare failed: %v", err)
		return
	}

	msgs, err := ch.Consume(queue, "", false, false, false, false, nil)
	if err != nil {
		log.Printf("[catalog-consumer] consume failed: %v", err)
		return
	}

	log.Printf("[catalog-consumer] started on %s queue %s", amqpURL, queue)

	for {
		select {
		case <-ctx.Done():
			log.Println("[catalog-consumer] graceful shutdown")
			return
		case msg, ok := <-msgs:
			if !ok {
				return
			}
			if processMessage(msg) {
				msg.Ack(false)
			} else {
				msg.Nack(false, false)
				log.Printf("[catalog-consumer] rejected msg id=%s (requeue=false)", msg.MessageId)
			}
		}
	}
}

func processMessage(msg amqp.Delivery) bool {
	// Catalog-side reservation / return on OrderCompensated (F-95)
	log.Printf("[catalog-consumer] received id=%s body=%s", msg.MessageId, string(msg.Body))
	// Placeholder: real SagaState logic per #48 / #95
	return true
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8082"
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	handler := newRootHandler(prometheus.DefaultRegisterer, prometheus.DefaultGatherer)

	mux := http.NewServeMux()
	mux.Handle("/live", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"UP"}`))
	}))
	mux.Handle("/", handler)

	srv := &http.Server{Addr: ":" + port, Handler: mux}

	go func() {
		<-ctx.Done()
		log.Println("SIGTERM received, graceful shutdown")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		srv.Shutdown(shutdownCtx)
	}()

	go startConsumer(ctx)

	log.Printf("=== Catalog Service started on port %s ===", port)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("Fatal: %v", err)
	}
}
