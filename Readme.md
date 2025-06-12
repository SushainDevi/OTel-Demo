
# OpenTelemetry Collector with Alert Processing Demo

This project is a demonstration of a custom telemetry collector written in Go, featuring:

- **OpenTelemetry metrics collection and Prometheus export**
- **Realistic metric simulation for multiple services**
- **Alerting based on configurable rules**
- **Alert enrichment, deduplication, and escalation**
- **HTTP API for metrics, alerts, and health checks**

## Features

- **Simulates metrics** for services like `web-server`, `api-gateway`, `database`, and `cache-service`
- **Alert rules** for CPU, memory, disk usage, and response time
- **Alert processing pipeline**: enrichment (adds team/owner info), deduplication (suppresses duplicates), and escalation (console output)
- **Prometheus-compatible metrics endpoint** (`/metrics`)
- **JSON API endpoints** for current metrics and active alerts

## Getting Started

### Prerequisites

- Go 1.18 or newer
- (Optional) Prometheus for scraping metrics

### Installation

1. **Install dependencies:**
   ```sh
   go mod tidy
   ```

### Running the Demo

```sh
go run otel-custom-demo/main.go
```

You should see output indicating the HTTP server is running and metrics are being collected.

### HTTP Endpoints

- **Prometheus Metrics:**  
  [http://localhost:8080/metrics](http://localhost:8080/metrics)

- **Current Metrics (JSON):**  
  [http://localhost:8080/current-metrics](http://localhost:8080/current-metrics)

- **Active Alerts (JSON):**  
  [http://localhost:8080/alerts](http://localhost:8080/alerts)

- **Health Check:**  
  [http://localhost:8080/health](http://localhost:8080/health)

## How It Works

- The main loop simulates metric collection for several services.
- Each metric is checked against alert rules (e.g., high CPU or memory).
- Alerts are enriched with metadata (team, environment, owner), deduplicated, and escalated (printed to console).
- All metrics and alerts are available via HTTP endpoints.

## Example Alert Output

```
🚨 [ALERT] warning - web-server
   Message: High CPU usage detected: 87.23
   Metric: cpu_usage_percent = 87.23 (threshold: 80.00)
   Team: Backend | Environment: production
   Time: 12:34:56
   --------------------------------------------------
```

## Customization

- **Alert Rules:**  
  Edit the `alertRules` slice in `NewTelemetryCollector()` to add or modify alerting logic.

- **Service Metadata:**  
  Update the `services` map in `NewAlertProcessor()` to change team, environment, or owner info.

- **Notification Channels:**  
  Currently, only console output is implemented. Extend the `NotificationChannel` interface to add email, Slack, etc.

## Stopping the Demo

Press `Ctrl+C` in your terminal to stop the server.


