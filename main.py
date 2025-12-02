import os
import time
import random
import atexit
from datetime import datetime, timedelta
try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError as exc:
    raise ImportError(
        "APScheduler is required for scheduling; install with `pip install apscheduler`."
    ) from exc
# Remove dotenv import - not needed for Render
from traceloop.sdk import Traceloop
from traceloop.sdk.decorators import workflow, task
import google.generativeai as genai
from opentelemetry.trace import get_current_span, Status, StatusCode
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
import logging
import signal
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Add health server
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass  # Suppress health check logs

def start_health_server():
    server = HTTPServer(('0.0.0.0', 8000), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Get credentials directly from environment (Render injects these)
KLOUDMATE_ENDPOINT = os.getenv("KLOUDMATE_ENDPOINT", "https://otel.kloudmate.com:4318")
KLOUDMATE_API_KEY = os.getenv("KLOUDMATE_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Validate required environment variables
if not KLOUDMATE_API_KEY:
    raise ValueError("KLOUDMATE_API_KEY environment variable is required")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable is required")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Initialize Traceloop with Kloudmate for traces
Traceloop.init(
    app_name="gemini-dashboard-metrics",
    api_endpoint=KLOUDMATE_ENDPOINT,
    headers={"authorization": KLOUDMATE_API_KEY}
)

# Set up metrics exporter and provider for OTLP to Kloudmate
metric_exporter = OTLPMetricExporter(
    endpoint=KLOUDMATE_ENDPOINT,
    headers={"authorization": KLOUDMATE_API_KEY}
)
metric_reader = PeriodicExportingMetricReader(
    metric_exporter,
    export_interval_millis=60000  # Export every 60 seconds
)
meter_provider = MeterProvider(metric_readers=[metric_reader])
otel_metrics.set_meter_provider(meter_provider)

# Get meter for instrumentation
meter = otel_metrics.get_meter(__name__)

# Create custom metrics
request_counter = meter.create_counter(
    name="gen_ai_request_total",
    description="Total number of LLM requests",
    unit="1"
)

error_counter = meter.create_counter(
    name="gen_ai_request_errors_total",
    description="Total number of failed LLM requests",
    unit="1"
)

token_counter = meter.create_counter(
    name="gen_ai_tokens_used",
    description="Total tokens consumed",
    unit="tokens"
)

cost_counter = meter.create_counter(
    name="gen_ai_cost_total",
    description="Total cost in USD",
    unit="USD"
)

latency_histogram = meter.create_histogram(
    name="llm_response_duration_ms",
    description="Response duration in milliseconds",
    unit="ms"
)

logger.info(f"🔧 Configuration:")
logger.info(f"   Endpoint: {KLOUDMATE_ENDPOINT}")
logger.info(f"   API Keys: ✓ Loaded")
logger.info("   Metrics: OTLP exporter active (every 60s)")
logger.info(f"   Environment: {'Production (Render)' if os.getenv('RENDER') else 'Local'}\n")

# Model rate limits and configurations
MODEL_CONFIGS = {
    "gemini-2.5-flash-lite": {
        "rpm": 15,           # Requests Per Minute
        "tpm": 250000,       # Tokens Per Minute
        "rpd": 1000,         # Requests Per Day
        "pricing": {"input": 0.10, "output": 0.40}
    },
    "gemini-2.0-flash-lite": {
        "rpm": 30,
        "tpm": 1000000,
        "rpd": 200,
        "pricing": {"input": 0.075, "output": 0.30}
    },
    "gemini-2.0-flash": {
        "rpm": 15,
        "tpm": 1000000,
        "rpd": 200,
        "pricing": {"input": 0.10, "output": 0.40}
    }
}

# Track usage per model
class UsageTracker:
    def __init__(self):
        self.reset_daily()
        self.reset_minute()
        self.last_daily_reset = datetime.now()
        self.last_minute_reset = datetime.now()
    
    def reset_daily(self):
        self.daily_requests = {model: 0 for model in MODEL_CONFIGS.keys()}
        self.daily_tokens = {model: 0 for model in MODEL_CONFIGS.keys()}
    
    def reset_minute(self):
        self.minute_requests = {model: 0 for model in MODEL_CONFIGS.keys()}
        self.minute_tokens = {model: 0 for model in MODEL_CONFIGS.keys()}
    
    def check_and_reset(self):
        now = datetime.now()
        # Reset daily counters
        if (now - self.last_daily_reset).total_seconds() >= 86400:
            self.reset_daily()
            self.last_daily_reset = now
            logger.info("🔄 Daily rate limits reset")
        
        # Reset minute counters
        if (now - self.last_minute_reset).total_seconds() >= 60:
            self.reset_minute()
            self.last_minute_reset = now
    
    def can_make_request(self, model: str, estimated_tokens: int = 500) -> bool:
        self.check_and_reset()
        config = MODEL_CONFIGS[model]
        
        # Check RPM limit
        if self.minute_requests[model] >= config["rpm"]:
            return False
        
        # Check TPM limit
        if self.minute_tokens[model] + estimated_tokens > config["tpm"]:
            return False
        
        # Check RPD limit
        if self.daily_requests[model] >= config["rpd"]:
            return False
        
        return True
    
    def record_request(self, model: str, tokens: int):
        self.minute_requests[model] += 1
        self.minute_tokens[model] += tokens
        self.daily_requests[model] += 1
        self.daily_tokens[model] += tokens
    
    def get_status(self):
        return {
            model: {
                "minute_requests": f"{self.minute_requests[model]}/{config['rpm']}",
                "minute_tokens": f"{self.minute_tokens[model]}/{config['tpm']}",
                "daily_requests": f"{self.daily_requests[model]}/{config['rpd']}"
            }
            for model, config in MODEL_CONFIGS.items()
        }

usage_tracker = UsageTracker()

@task(name="gemini_generation_with_metrics")
def generate_with_full_metrics(prompt: str, model_name: str, temperature: float = 1.0, max_tokens: int = 500):
    """
    Generate content with metrics tracking and rate limit management
    """
    # Estimate tokens for rate limit check
    estimated_tokens = len(prompt.split()) * 1.5 + max_tokens
    
    # Check rate limits before making request
    if not usage_tracker.can_make_request(model_name, int(estimated_tokens)):
        logger.warning(f"⚠️ Rate limit reached for {model_name}, skipping request")
        return None
    
    start_time = time.time()
    current_span = get_current_span()
    
    # Set span attributes
    current_span.set_attribute("gen_ai.system", "google")
    current_span.set_attribute("gen_ai.request.model", model_name)
    current_span.set_attribute("gen_ai.request.temperature", temperature)
    current_span.set_attribute("gen_ai.request.max_tokens", max_tokens)
    
    logger.info(f"📤 LLM Request: model={model_name}")
    
    try:
        generation_config = genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config=generation_config
        )
        
        response = model.generate_content(prompt)
        duration_ms = (time.time() - start_time) * 1000
        
        # Get token usage
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            prompt_tokens = response.usage_metadata.prompt_token_count
            completion_tokens = response.usage_metadata.candidates_token_count
            total_tokens = response.usage_metadata.total_token_count
        else:
            prompt_tokens = len(prompt.split()) * 1.3
            completion_tokens = len(response.text.split()) * 1.3
            total_tokens = prompt_tokens + completion_tokens
        
        # Record usage
        usage_tracker.record_request(model_name, int(total_tokens))
        
        # Calculate cost
        model_pricing = MODEL_CONFIGS[model_name]["pricing"]
        input_cost = (prompt_tokens / 1_000_000) * model_pricing["input"]
        output_cost = (completion_tokens / 1_000_000) * model_pricing["output"]
        estimated_cost = input_cost + output_cost
        
        # Set trace attributes
        current_span.set_attribute("gen_ai.usage.prompt_tokens", int(prompt_tokens))
        current_span.set_attribute("gen_ai.usage.completion_tokens", int(completion_tokens))
        current_span.set_attribute("gen_ai.usage.total_tokens", int(total_tokens))
        current_span.set_attribute("llm_response_duration_ms", duration_ms)
        current_span.set_attribute("gen_ai.usage.cost", estimated_cost)
        
        # Record metrics
        request_counter.add(1, {"model": model_name, "status": "success"})
        token_counter.add(int(prompt_tokens), {"model": model_name, "type": "prompt"})
        token_counter.add(int(completion_tokens), {"model": model_name, "type": "completion"})
        cost_counter.add(estimated_cost, {"model": model_name})
        latency_histogram.record(duration_ms, {"model": model_name})
        
        current_span.set_status(Status(StatusCode.OK))
        
        logger.info(
            f"✅ Success: {model_name} | "
            f"tokens={int(total_tokens)} | "
            f"duration={duration_ms:.0f}ms | "
            f"cost=${estimated_cost:.6f}"
        )
        
        return {
            "text": response.text,
            "metrics": {
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "total_tokens": int(total_tokens),
                "duration_ms": duration_ms,
                "cost": estimated_cost,
                "model": model_name,
            }
        }
        
    except Exception as e:
        error_duration = (time.time() - start_time) * 1000
        
        error_counter.add(1, {"model": model_name})
        request_counter.add(1, {"model": model_name, "status": "error"})
        latency_histogram.record(error_duration, {"model": model_name})
        
        current_span.set_status(Status(StatusCode.ERROR, str(e)))
        current_span.set_attribute("error", True)
        current_span.record_exception(e)
        
        logger.error(f"❌ Error: {model_name} - {type(e).__name__}")
        return None

@task(name="vector_db_retrieval")
def simulate_vector_db_lookup(query: str):
    """Simulate vector database retrieval"""
    current_span = get_current_span()
    start_time = time.time()
    
    retrieval_time = random.uniform(0.05, 0.2)
    time.sleep(retrieval_time)
    
    num_results = random.randint(2, 5)
    duration_ms = (time.time() - start_time) * 1000
    
    current_span.set_attribute("db.results.count", num_results)
    current_span.set_attribute("llm_response_duration_ms", duration_ms)
    
    return {
        "results": [f"Context document {i}" for i in range(num_results)],
        "duration_ms": duration_ms
    }

@workflow(name="llm_chat_workflow")
def execute_llm_request(user_query: str, model_name: str, temperature: float, use_rag: bool = False):
    """Single LLM request workflow with optional RAG"""
    prompt = user_query
    if use_rag:
        context = simulate_vector_db_lookup(user_query)
        prompt = f"Context: {context['results'][0]}\n\nQuestion: {user_query}"
    
    result = generate_with_full_metrics(
        prompt=prompt,
        model_name=model_name,
        temperature=temperature,
        max_tokens=random.randint(100, 400)
    )
    
    return result

def generate_dense_dashboard_traffic():
    """
    Generate DENSE traffic to populate dashboard maximally while respecting rate limits
    
    Strategy:
    - gemini-2.0-flash-lite: 30 RPM, 200 RPD → Use heavily (high volume)
    - gemini-2.5-flash-lite: 15 RPM, 1000 RPD → Use moderately  
    - gemini-2.0-flash: 15 RPM, 200 RPD → Use lightly (expensive)
    
    Per 5-minute cycle:
    - 2.0-flash-lite: ~25 requests (30 RPM max, stay under limit)
    - 2.5-flash-lite: ~12 requests (15 RPM max)
    - 2.0-flash: ~8 requests (15 RPM max, save for premium queries)
    
    Total: ~45 requests per 5 minutes = ~540 requests per hour
    """
    
    logger.info("="*80)
    logger.info("🚀 GENERATING DENSE DASHBOARD DATA")
    logger.info("="*80)
    
    # Show current usage status
    status = usage_tracker.get_status()
    logger.info("\n📊 Current Rate Limit Status:")
    for model, stats in status.items():
        logger.info(f"   {model}:")
        logger.info(f"      Minute: {stats['minute_requests']} | Tokens: {stats['minute_tokens']} | Daily: {stats['daily_requests']}")
    logger.info("")
    
    queries = [
        "Explain machine learning in simple terms",
        "What are the best practices for API design?",
        "How does a neural network work?",
        "Write a Python function to sort a list",
        "What is the difference between REST and GraphQL?",
        "Explain quantum computing briefly",
        "How to optimize database queries?",
        "What are microservices advantages?",
        "Describe the SOLID principles",
        "How does Docker containerization work?",
        "What is the CAP theorem?",
        "Explain async/await in Python",
        "What are design patterns?",
        "How to implement JWT authentication?",
        "Difference between SQL and NoSQL?",
        "Explain CI/CD pipeline best practices",
    ]
    
    total_successful = 0
    total_skipped = 0
    
    # PHASE 1: High-volume with gemini-2.0-flash-lite (30 RPM limit)
    logger.info("🔵 Phase 1: High-Volume Traffic (gemini-2.0-flash-lite)")
    logger.info("   Target: 25 requests | Rate: 30 RPM")
    phase1_success = 0
    
    for i in range(25):
        result = execute_llm_request(
            user_query=random.choice(queries),
            model_name="gemini-2.0-flash-lite",
            temperature=random.uniform(0.5, 0.8),
            use_rag=random.choice([True, False])
        )
        if result:
            phase1_success += 1
            total_successful += 1
        else:
            total_skipped += 1
        
        time.sleep(0.15)  # Fast pace to maximize throughput
        
        if (i + 1) % 5 == 0:
            logger.info(f"   ✓ {phase1_success}/{i + 1} successful")
    
    logger.info(f"   ✅ Phase 1 Complete: {phase1_success}/25 requests")
    
    # PHASE 2: Moderate volume with gemini-2.5-flash-lite (15 RPM limit)
    logger.info("\n🟢 Phase 2: Moderate Traffic (gemini-2.5-flash-lite)")
    logger.info("   Target: 12 requests | Rate: 15 RPM")
    phase2_success = 0
    
    for i in range(12):
        # Mix of short and long queries for token diversity
        if i < 6:
            query = random.choice(queries[:4])  # Short queries
        else:
            query = f"{random.choice(queries)}. Provide detailed explanation with examples."  # Long queries
        
        result = execute_llm_request(
            user_query=query,
            model_name="gemini-2.5-flash-lite",
            temperature=random.uniform(0.6, 0.9),
            use_rag=random.choice([True, False, False])  # 33% RAG
        )
        if result:
            phase2_success += 1
            total_successful += 1
        else:
            total_skipped += 1
        
        time.sleep(0.25)  # Moderate pace
        
        if (i + 1) % 4 == 0:
            logger.info(f"   ✓ {phase2_success}/{i + 1} successful")
    
    logger.info(f"   ✅ Phase 2 Complete: {phase2_success}/12 requests")
    
    # PHASE 3: Premium queries with gemini-2.0-flash (15 RPM limit, use sparingly)
    logger.info("\n🔴 Phase 3: Premium Traffic (gemini-2.0-flash)")
    logger.info("   Target: 8 requests | Rate: 15 RPM")
    phase3_success = 0
    
    for i in range(8):
        # Only complex queries for premium model
        complex_query = f"{random.choice(queries)}. Provide comprehensive analysis with multiple perspectives and detailed examples."
        
        result = execute_llm_request(
            user_query=complex_query,
            model_name="gemini-2.0-flash",
            temperature=random.uniform(0.7, 1.0),
            use_rag=True  # Always use RAG for premium
        )
        if result:
            phase3_success += 1
            total_successful += 1
        else:
            total_skipped += 1
        
        time.sleep(0.3)  # Slower pace for premium
        
        if (i + 1) % 3 == 0:
            logger.info(f"   ✓ {phase3_success}/{i + 1} successful")
    
    logger.info(f"   ✅ Phase 3 Complete: {phase3_success}/8 requests")
    
    # PHASE 4: Mixed workload for realistic patterns
    logger.info("\n🟡 Phase 4: Mixed Realistic Workload")
    logger.info("   Target: 20 requests across all models")
    phase4_success = 0
    
    # Weighted model selection (favor cheaper models)
    model_weights = [
        ("gemini-2.0-flash-lite", 0.6),    # 60% - cheapest, highest limit
        ("gemini-2.5-flash-lite", 0.3),    # 30% - balanced
        ("gemini-2.0-flash", 0.1),         # 10% - premium, use sparingly
    ]
    
    for i in range(20):
        # Weighted random selection
        model = random.choices(
            [m[0] for m in model_weights],
            weights=[m[1] for m in model_weights]
        )[0]
        
        result = execute_llm_request(
            user_query=random.choice(queries),
            model_name=model,
            temperature=random.uniform(0.4, 0.9),
            use_rag=random.random() < 0.4  # 40% RAG
        )
        if result:
            phase4_success += 1
            total_successful += 1
        else:
            total_skipped += 1
        
        time.sleep(0.2)
        
        if (i + 1) % 5 == 0:
            logger.info(f"   ✓ {phase4_success}/{i + 1} successful")
    
    logger.info(f"   ✅ Phase 4 Complete: {phase4_success}/20 requests")
    
    # PHASE 5: RAG-intensive workload
    logger.info("\n🟣 Phase 5: RAG-Intensive Workload")
    logger.info("   Target: 15 requests with 100% RAG")
    phase5_success = 0
    
    for i in range(15):
        model = random.choice(["gemini-2.0-flash-lite", "gemini-2.5-flash-lite"])
        
        result = execute_llm_request(
            user_query=random.choice(queries),
            model_name=model,
            temperature=random.uniform(0.5, 0.8),
            use_rag=True  # 100% RAG
        )
        if result:
            phase5_success += 1
            total_successful += 1
        else:
            total_skipped += 1
        
        time.sleep(0.25)
    
    logger.info(f"   ✅ Phase 5 Complete: {phase5_success}/15 requests")
    
    # PHASE 6: Error testing (small amount)
    logger.info("\n⚠️ Phase 6: Error Scenarios")
    for i in range(2):
        try:
            generate_with_full_metrics(
                prompt=random.choice(queries),
                model_name="invalid-model-test",
                temperature=1.0,
                max_tokens=100
            )
        except:
            pass
        time.sleep(0.2)
    logger.info("   ✅ Error scenarios logged")
    
    # Final Summary
    logger.info("\n" + "="*80)
    logger.info("✅ DENSE TRAFFIC GENERATION COMPLETE")
    logger.info("="*80)
    logger.info(f"\n📊 Execution Summary:")
    logger.info(f"   Total Attempted: {total_successful + total_skipped}")
    logger.info(f"   Successful: {total_successful}")
    logger.info(f"   Rate Limited: {total_skipped}")
    logger.info(f"   Success Rate: {(total_successful/(total_successful + total_skipped)*100):.1f}%")
    
    # Show updated usage status
    status = usage_tracker.get_status()
    logger.info(f"\n📈 Updated Rate Limit Status:")
    for model, stats in status.items():
        logger.info(f"   {model}:")
        logger.info(f"      Minute: {stats['minute_requests']} | Daily: {stats['daily_requests']}")
    
    logger.info("\n" + "="*80)

def scheduled_task():
    """Task to run every 5 minutes with dense traffic generation"""
    try:
        logger.info("\n" + "🔄"*40)
        logger.info("🔄 SCHEDULED TASK STARTING")
        logger.info("🔄"*40 + "\n")
        
        generate_dense_dashboard_traffic()
        
        logger.info("\n" + "✅"*40)
        logger.info("✅ SCHEDULED TASK COMPLETED")
        logger.info("✅"*40 + "\n")
        logger.info(f"⏳ Next run in 5 minutes\n")
        
    except Exception as e:
        logger.error(f"❌ SCHEDULED TASK ERROR: {e}", exc_info=True)

def main():
    """Main function with continuous scheduling and health endpoint"""
    print("="*80)
    print("DENSE LLM Observability - 24/7 Generator")
    print("="*80)
    print("\nRate Limit Aware Traffic Generation:")
    print("  • gemini-2.0-flash-lite: 30 RPM, 200 RPD (High Volume)")
    print("  • gemini-2.5-flash-lite: 15 RPM, 1000 RPD (Moderate)")
    print("  • gemini-2.0-flash: 15 RPM, 200 RPD (Premium)")
    print("\nTarget: ~80 requests per 5 minutes")
    print("Daily Total: ~2,300 requests (spread across models)")
    print("\n" + "="*80 + "\n")

    # Start health server in background
    start_health_server()
    logger.info("✅ Health endpoint running on :8000/health")
    
    # Create scheduler
    scheduler = BackgroundScheduler()
    
    # Schedule task every 5 minutes
    scheduler.add_job(
        func=scheduled_task,
        trigger="interval",
        minutes=5,
        id="dense_data_generation",
        name="Generate dense dashboard data",
        replace_existing=True
    )
    
    # Run initial task
    logger.info("Running initial dense traffic generation...\n")
    scheduled_task()
    
    # Start scheduler
    scheduler.start()
    
    logger.info("="*80)
    logger.info("✅ SCHEDULER ACTIVE - RUNNING 24/7")
    logger.info("="*80)
    logger.info(f"Start time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Interval: Every 5 minutes")
    logger.info(f"Status: ACTIVE")
    logger.info("="*80 + "\n")

    # Graceful shutdown handling
    def shutdown_handler(signum, frame):
        logger.info("Received shutdown signal. Shutting down gracefully...")
        scheduler.shutdown(wait=True)
        logger.info("Scheduler stopped. Goodbye!")
        exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")

if __name__ == "__main__":
    main()