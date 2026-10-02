package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

// The previous version of this file declared its own handlers and asserted on
// those, so it passed no matter what the service did. Every test here drives
// the handlers that main() actually mounts.

func TestHealthEndpoint(t *testing.T) {
	rec := httptest.NewRecorder()
	healthHandler()(rec, httptest.NewRequest(http.MethodGet, "/health", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/json" {
		t.Fatalf("expected application/json, got %q", got)
	}

	var payload struct {
		Status  string `json:"status"`
		Service string `json:"service"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if payload.Status != "UP" || payload.Service != "catalog-service" {
		t.Fatalf("unexpected payload: %+v", payload)
	}
}

// catalog has no external dependencies, so readiness is liveness. If someone
// later wires a database in, this test is the reminder that readyHandler was
// supposed to start reporting real state.
func TestReadyEndpointReportsTheSameAsHealth(t *testing.T) {
	health := httptest.NewRecorder()
	healthHandler()(health, httptest.NewRequest(http.MethodGet, "/health", nil))

	ready := httptest.NewRecorder()
	readyHandler()(ready, httptest.NewRequest(http.MethodGet, "/ready", nil))

	if ready.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", ready.Code)
	}
	if ready.Body.String() != health.Body.String() {
		t.Fatalf("/ready body %q differs from /health body %q",
			ready.Body.String(), health.Body.String())
	}
}

// sumRequestsTotal reads the counter straight from the registry instead of
// scraping text, so an unexpected label set shows up as a wrong number rather
// than as a string match that quietly stops matching.
func sumRequestsTotal(t *testing.T, gatherer prometheus.Gatherer) float64 {
	t.Helper()

	families, err := gatherer.Gather()
	if err != nil {
		t.Fatalf("gather metrics: %v", err)
	}

	found := false
	total := 0.0
	for _, family := range families {
		if family.GetName() != "http_requests_total" {
			continue
		}
		found = true
		for _, metric := range family.GetMetric() {
			total += metric.GetCounter().GetValue()
		}
	}

	if !found {
		t.Fatal("http_requests_total was never registered")
	}
	return total
}

// kubelet polls both probes every periodSeconds. Counting that traffic would
// make http_requests_total almost entirely probe noise, and /products would be
// a rounding error.
func TestProbeTrafficIsNotCountedAsBusinessTraffic(t *testing.T) {
	registry := prometheus.NewRegistry()
	handler := newRootHandler(registry, registry)

	for i := 0; i < 3; i++ {
		for _, path := range []string{"/health", "/ready"} {
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("%s: expected 200, got %d", path, rec.Code)
			}
		}
	}

	if got := sumRequestsTotal(t, registry); got != 0 {
		t.Fatalf("probe traffic must not be counted, got %v", got)
	}

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/products", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("/products: expected 200, got %d", rec.Code)
	}

	if got := sumRequestsTotal(t, registry); got != 1 {
		t.Fatalf("expected only the /products request to be counted, got %v", got)
	}
}

// A scrape must not inflate the counters it is about to report.
func TestMetricsScrapeIsNotCounted(t *testing.T) {
	registry := prometheus.NewRegistry()
	handler := newRootHandler(registry, registry)

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if got := sumRequestsTotal(t, registry); got != 0 {
		t.Fatalf("scrape must not be counted, got %v", got)
	}
}

func TestProductsEndpoint(t *testing.T) {
	rec := httptest.NewRecorder()
	newMux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/products", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var products []Product
	if err := json.Unmarshal(rec.Body.Bytes(), &products); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if len(products) != 2 {
		t.Fatalf("expected 2 products, got %d", len(products))
	}
	for _, product := range products {
		if product.Price <= 0 {
			t.Fatalf("product price must be positive, got %v", product.Price)
		}
	}
}