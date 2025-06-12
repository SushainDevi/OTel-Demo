package main

import (
	"context"
	"crypto/md5"
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"strings"
	"sync"
	"time"

	// Only import if you need to register custom Prometheus metrics (not needed for basic OTel usage)
	// "github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelprom "go.opentelemetry.io/otel/exporters/prometheus"
	otelmetric "go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
)

// Alert represents a processed alert
type Alert struct {
	ID          string            `json:"id"`
	Timestamp   time.Time         `json:"timestamp"`
	Severity    string            `json:"severity"`
	Service     string            `json:"service"`
	Message     string            `json:"message"`
	Labels      map[string]string `json:"labels"`
	Fingerprint string            `json:"fingerprint"`
	Metadata    map[string]string `json:"metadata,omitempty"`
	MetricName  string            `json:"metric_name"`
	MetricValue float64           `json:"metric_value"`
	Threshold   float64           `json:"threshold"`
}

// MetricData represents incoming telemetry data
type MetricData struct {
	Name      string            `json:"name"`
	Value     float64           `json:"value"`
	Timestamp time.Time         `json:"timestamp"`
	Labels    map[string]string `json:"labels"`
}

// AlertRule defines when to trigger alerts
type AlertRule struct {
	MetricName string  `json:"metric_name"`
	Operator   string  `json:"operator"` // >, <, >=, <=, ==
	Threshold  float64 `json:"threshold"`
	Severity   string  `json:"severity"`
	Message    string  `json:"message"`
}

// TelemetryCollector simulates a basic telemetry collector
type TelemetryCollector struct {
	alertProcessor *AlertProcessor
	alertRules     []AlertRule
	metrics        map[string]*MetricData
	mu             sync.RWMutex

	// OpenTelemetry metrics
	meterProvider *sdkmetric.MeterProvider
	meter         otelmetric.Meter
	alertCounter  otelmetric.Int64Counter
	metricGauge   otelmetric.Float64Gauge
}

// AlertProcessor handles alert processing (from previous demo)
type AlertProcessor struct {
	enricher     *Enricher
	deduplicator *Deduplicator
	escalator    *Escalator
	alertCache   map[string]*Alert
	mu           sync.RWMutex
}

// Enricher adds context to alerts
type Enricher struct {
	services map[string]ServiceInfo
}

type ServiceInfo struct {
	Team        string
	Environment string
	Criticality string
	Owner       string
}

// Deduplicator handles alert grouping
type Deduplicator struct {
	timeWindow time.Duration
	cache      map[string][]*Alert
	mu         sync.RWMutex
}

// Escalator handles alert routing
type Escalator struct {
	channels map[string]NotificationChannel
}

type NotificationChannel interface {
	Send(alert *Alert) error
	Name() string
}

// ConsoleChannel implements console notifications
type ConsoleChannel struct {
	name string
}

func (c *ConsoleChannel) Send(alert *Alert) error {
	fmt.Printf("\n🚨 [ALERT] %s - %s\n", alert.Severity, alert.Service)
	fmt.Printf("   Message: %s\n", alert.Message)
	fmt.Printf("   Metric: %s = %.2f (threshold: %.2f)\n",
		alert.MetricName, alert.MetricValue, alert.Threshold)
	fmt.Printf("   Team: %s | Environment: %s\n",
		alert.Metadata["team"], alert.Metadata["environment"])
	fmt.Printf("   Time: %s\n", alert.Timestamp.Format("15:04:05"))
	fmt.Printf("   %s\n", strings.Repeat("-", 50))
	return nil
}

func (c *ConsoleChannel) Name() string {
	return c.name
}

// NewTelemetryCollector creates a new collector with alert processing
func NewTelemetryCollector() (*TelemetryCollector, error) {
	// Initialize OpenTelemetry Prometheus exporter
	exporter, err := otelprom.New()
	if err != nil {
		return nil, fmt.Errorf("failed to create prometheus exporter: %w", err)
	}

	provider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(exporter))
	otel.SetMeterProvider(provider)

	meter := provider.Meter("telemetry-collector")

	// Create metrics
	alertCounter, err := meter.Int64Counter(
		"alerts_total",
		otelmetric.WithDescription("Total number of alerts processed"),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create alert counter: %w", err)
	}

	metricGauge, err := meter.Float64Gauge(
		"system_metrics",
		otelmetric.WithDescription("System metrics being monitored"),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create metric gauge: %w", err)
	}

	// Initialize alert processor
	alertProcessor := NewAlertProcessor()

	// Define basic alert rules
	alertRules := []AlertRule{
		{
			MetricName: "cpu_usage_percent",
			Operator:   ">",
			Threshold:  80.0,
			Severity:   "warning",
			Message:    "High CPU usage detected",
		},
		{
			MetricName: "cpu_usage_percent",
			Operator:   ">",
			Threshold:  95.0,
			Severity:   "critical",
			Message:    "Critical CPU usage detected",
		},
		{
			MetricName: "memory_usage_percent",
			Operator:   ">",
			Threshold:  85.0,
			Severity:   "warning",
			Message:    "High memory usage detected",
		},
		{
			MetricName: "disk_usage_percent",
			Operator:   ">",
			Threshold:  90.0,
			Severity:   "critical",
			Message:    "Critical disk usage detected",
		},
		{
			MetricName: "response_time_ms",
			Operator:   ">",
			Threshold:  1000.0,
			Severity:   "warning",
			Message:    "High response time detected",
		},
	}

	return &TelemetryCollector{
		alertProcessor: alertProcessor,
		alertRules:     alertRules,
		metrics:        make(map[string]*MetricData),
		meterProvider:  provider,
		meter:          meter,
		alertCounter:   alertCounter,
		metricGauge:    metricGauge,
	}, nil
}

// NewAlertProcessor creates alert processor (simplified from previous demo)
func NewAlertProcessor() *AlertProcessor {
	services := map[string]ServiceInfo{
		"web-server":    {Team: "Backend", Environment: "production", Criticality: "high", Owner: "backend@company.com"},
		"api-gateway":   {Team: "Platform", Environment: "production", Criticality: "critical", Owner: "platform@company.com"},
		"database":      {Team: "Database", Environment: "production", Criticality: "critical", Owner: "dba@company.com"},
		"cache-service": {Team: "Infrastructure", Environment: "production", Criticality: "medium", Owner: "infra@company.com"},
	}

	enricher := &Enricher{services: services}
	deduplicator := &Deduplicator{
		timeWindow: 2 * time.Minute, // Shorter window for demo
		cache:      make(map[string][]*Alert),
	}

	channels := map[string]NotificationChannel{
		"console": &ConsoleChannel{name: "CONSOLE"},
	}

	escalator := &Escalator{channels: channels}

	return &AlertProcessor{
		enricher:     enricher,
		deduplicator: deduplicator,
		escalator:    escalator,
		alertCache:   make(map[string]*Alert),
	}
}

// CollectMetric processes incoming telemetry data
func (tc *TelemetryCollector) CollectMetric(ctx context.Context, metric *MetricData) error {
	// Store the metric
	tc.mu.Lock()
	metricKey := fmt.Sprintf("%s_%s", metric.Name, getServiceFromLabels(metric.Labels))
	tc.metrics[metricKey] = metric
	tc.mu.Unlock()

	// Record metric in OpenTelemetry
	attrs := make([]attribute.KeyValue, 0, len(metric.Labels)+1)
	attrs = append(attrs, attribute.String("metric_name", metric.Name))

	for k, v := range metric.Labels {
		attrs = append(attrs, attribute.String(k, v))
	}

	tc.metricGauge.Record(ctx, metric.Value, otelmetric.WithAttributes(attrs...))

	// Check alert rules
	for _, rule := range tc.alertRules {
		if rule.MetricName == metric.Name {
			if tc.evaluateRule(rule, metric.Value) {
				alert := tc.createAlert(rule, metric)
				if err := tc.alertProcessor.ProcessAlert(alert); err != nil {
					log.Printf("Error processing alert: %v", err)
				}

				// Record alert in metrics
				tc.alertCounter.Add(ctx, 1, otelmetric.WithAttributes(
					attribute.String("severity", alert.Severity),
					attribute.String("service", alert.Service),
					attribute.String("metric", alert.MetricName),
				))
			}
		}
	}

	return nil
}

// evaluateRule checks if a metric value triggers an alert rule
func (tc *TelemetryCollector) evaluateRule(rule AlertRule, value float64) bool {
	switch rule.Operator {
	case ">":
		return value > rule.Threshold
	case "<":
		return value < rule.Threshold
	case ">=":
		return value >= rule.Threshold
	case "<=":
		return value <= rule.Threshold
	case "==":
		return value == rule.Threshold
	default:
		return false
	}
}

// createAlert converts a triggered rule into an alert
func (tc *TelemetryCollector) createAlert(rule AlertRule, metric *MetricData) *Alert {
	service := getServiceFromLabels(metric.Labels)
	if service == "" {
		service = "unknown-service"
	}

	return &Alert{
		ID:          fmt.Sprintf("alert-%d", time.Now().Unix()),
		Timestamp:   metric.Timestamp,
		Severity:    rule.Severity,
		Service:     service,
		Message:     fmt.Sprintf("%s: %.2f", rule.Message, metric.Value),
		Labels:      metric.Labels,
		MetricName:  metric.Name,
		MetricValue: metric.Value,
		Threshold:   rule.Threshold,
	}
}

// getServiceFromLabels extracts service name from metric labels
func getServiceFromLabels(labels map[string]string) string {
	if service, exists := labels["service"]; exists {
		return service
	}
	if job, exists := labels["job"]; exists {
		return job
	}
	if instance, exists := labels["instance"]; exists {
		return instance
	}
	return "unknown"
}

// ProcessAlert handles alert processing pipeline
func (ap *AlertProcessor) ProcessAlert(alert *Alert) error {
	// Enrich the alert
	enrichedAlert, err := ap.enricher.Enrich(alert)
	if err != nil {
		return fmt.Errorf("enrichment failed: %w", err)
	}

	// Generate fingerprint
	enrichedAlert.Fingerprint = ap.generateFingerprint(enrichedAlert)

	// Check for duplicates
	if ap.deduplicator.IsDuplicate(enrichedAlert) {
		fmt.Printf("🔄 Duplicate alert suppressed: %s\n", enrichedAlert.ID)
		return nil
	}

	// Store in cache
	ap.mu.Lock()
	ap.alertCache[enrichedAlert.ID] = enrichedAlert
	ap.mu.Unlock()

	// Escalate
	return ap.escalator.Escalate(enrichedAlert)
}

// Enrich adds contextual metadata
func (e *Enricher) Enrich(alert *Alert) (*Alert, error) {
	enriched := *alert
	enriched.Metadata = make(map[string]string)

	if serviceInfo, exists := e.services[alert.Service]; exists {
		enriched.Metadata["team"] = serviceInfo.Team
		enriched.Metadata["environment"] = serviceInfo.Environment
		enriched.Metadata["criticality"] = serviceInfo.Criticality
		enriched.Metadata["owner"] = serviceInfo.Owner
	} else {
		enriched.Metadata["team"] = "Unknown"
		enriched.Metadata["environment"] = "production"
		enriched.Metadata["criticality"] = "medium"
		enriched.Metadata["owner"] = "oncall@company.com"
	}

	return &enriched, nil
}

// IsDuplicate checks for duplicate alerts
func (d *Deduplicator) IsDuplicate(alert *Alert) bool {
	d.mu.Lock()
	defer d.mu.Unlock()

	fingerprint := alert.Fingerprint
	now := time.Now()

	if alerts, exists := d.cache[fingerprint]; exists {
		for _, existingAlert := range alerts {
			if now.Sub(existingAlert.Timestamp) < d.timeWindow {
				return true
			}
		}
	}

	d.cache[fingerprint] = append(d.cache[fingerprint], alert)
	return false
}

// Escalate routes alerts
func (e *Escalator) Escalate(alert *Alert) error {
	return e.channels["console"].Send(alert)
}

// generateFingerprint creates alert fingerprint
func (ap *AlertProcessor) generateFingerprint(alert *Alert) string {
	data := fmt.Sprintf("%s:%s:%s", alert.Service, alert.MetricName, alert.Severity)
	hash := md5.Sum([]byte(data))
	return fmt.Sprintf("%x", hash)[:8]
}

// GenerateRealisticMetrics creates realistic telemetry data
func GenerateRealisticMetrics() []*MetricData {
	services := []string{"web-server", "api-gateway", "database", "cache-service"}
	var metrics []*MetricData

	for _, service := range services {
		now := time.Now()

		// CPU Usage (0-100%)
		cpuUsage := 20 + rand.Float64()*60 // Base 20-80%
		if rand.Float64() < 0.3 {          // 30% chance of high CPU
			cpuUsage = 85 + rand.Float64()*15 // 85-100%
		}

		metrics = append(metrics, &MetricData{
			Name:      "cpu_usage_percent",
			Value:     cpuUsage,
			Timestamp: now,
			Labels: map[string]string{
				"service":  service,
				"instance": fmt.Sprintf("%s-01", service),
				"region":   "us-west-2",
			},
		})

		// Memory Usage (0-100%)
		memUsage := 30 + rand.Float64()*50 // Base 30-80%
		if rand.Float64() < 0.2 {          // 20% chance of high memory
			memUsage = 85 + rand.Float64()*15
		}

		metrics = append(metrics, &MetricData{
			Name:      "memory_usage_percent",
			Value:     memUsage,
			Timestamp: now,
			Labels: map[string]string{
				"service":  service,
				"instance": fmt.Sprintf("%s-01", service),
				"region":   "us-west-2",
			},
		})

		// Response Time (50-2000ms)
		responseTime := 100 + rand.Float64()*300           // Base 100-400ms
		if service == "database" && rand.Float64() < 0.4 { // DB can be slow
			responseTime = 800 + rand.Float64()*1200 // 800-2000ms
		}

		metrics = append(metrics, &MetricData{
			Name:      "response_time_ms",
			Value:     responseTime,
			Timestamp: now,
			Labels: map[string]string{
				"service":  service,
				"instance": fmt.Sprintf("%s-01", service),
				"endpoint": "/api/health",
			},
		})

		// Disk Usage (only for database)
		if service == "database" {
			diskUsage := 70 + rand.Float64()*25 // 70-95%
			metrics = append(metrics, &MetricData{
				Name:      "disk_usage_percent",
				Value:     diskUsage,
				Timestamp: now,
				Labels: map[string]string{
					"service":  service,
					"instance": fmt.Sprintf("%s-01", service),
					"mount":    "/data",
				},
			})
		}
	}

	return metrics
}

// HTTPHandler provides HTTP endpoints for metrics and health
func (tc *TelemetryCollector) HTTPHandler() http.Handler {
	mux := http.NewServeMux()

	// Prometheus metrics endpoint
	mux.Handle("/metrics", promhttp.Handler())

	// Current metrics endpoint
	mux.HandleFunc("/current-metrics", func(w http.ResponseWriter, r *http.Request) {
		tc.mu.RLock()
		defer tc.mu.RUnlock()

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(tc.metrics)
	})

	// Alert status endpoint
	mux.HandleFunc("/alerts", func(w http.ResponseWriter, r *http.Request) {
		tc.alertProcessor.mu.RLock()
		defer tc.alertProcessor.mu.RUnlock()

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(tc.alertProcessor.alertCache)
	})

	// Health check
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("OK"))
	})

	return mux
}

func main() {
	fmt.Println("🚀 OpenTelemetry Collector with Alert Processing Demo")
	fmt.Println(strings.Repeat("=", 60))

	// Initialize collector
	collector, err := NewTelemetryCollector()
	if err != nil {
		log.Fatalf("Failed to create collector: %v", err)
	}

	// Start HTTP server for metrics and API
	go func() {
		fmt.Println("📊 Starting HTTP server on :8080")
		fmt.Println("   - Metrics: http://localhost:8080/metrics")
		fmt.Println("   - Current metrics: http://localhost:8080/current-metrics")
		fmt.Println("   - Active alerts: http://localhost:8080/alerts")
		fmt.Println("   - Health: http://localhost:8080/health")

		if err := http.ListenAndServe(":8080", collector.HTTPHandler()); err != nil {
			log.Fatalf("HTTP server failed: %v", err)
		}
	}()

	// Wait for HTTP server to start
	time.Sleep(2 * time.Second)

	ctx := context.Background()

	fmt.Println(strings.Repeat("=", 60))
	fmt.Println("📈 Starting metric collection and alert processing...")
	fmt.Println(strings.Repeat("=", 60))

	// Simulate continuous metric collection
	for i := 0; i < 20; i++ {
		fmt.Printf("\n🔄 Collection cycle %d\n", i+1)

		// Generate realistic metrics
		metrics := GenerateRealisticMetrics()

		// Process each metric
		for _, metric := range metrics {
			if err := collector.CollectMetric(ctx, metric); err != nil {
				log.Printf("Error collecting metric: %v", err)
			}
		}

		// Show current metrics summary
		collector.mu.RLock()
		fmt.Printf("📊 Collected %d metrics\n", len(collector.metrics))
		collector.mu.RUnlock()

		collector.alertProcessor.mu.RLock()
		alertCount := len(collector.alertProcessor.alertCache)
		collector.alertProcessor.mu.RUnlock()

		if alertCount > 0 {
			fmt.Printf("🚨 Active alerts: %d\n", alertCount)
		}

		// Wait before next collection
		time.Sleep(3 * time.Second)
	}

	fmt.Println(strings.Repeat("=", 60))
	fmt.Println("✅ Demo completed!")
	fmt.Println("💡 Check the HTTP endpoints for detailed metrics and alerts")
	fmt.Println("   Press Ctrl+C to exit")

	// Keep server running
	select {}
}
