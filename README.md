# Minimal RAG with Redis Answer Caching

**Use case:** Question answering over the **Database Performance and Scaling Training Guide** (iCog Labs).

The application uses the guide's text in `data/docs/database_scaling_guide.md` as its knowledge corpus. It answers questions about indexing, read scaling, write scaling, caching, and related topics, returning source references for the passages used.

## Mandatory Workflow

Implemented in `app/rag.py` → `RAGService.ask()`:

```text
Question
   |
   v
Redis Cache Lookup (BEFORE retrieval or generation)
   |
   +-- HIT
   |    |
   |    +--> Return cached answer + original sources
   |    +--> Skip retrieval
   |    +--> Skip generation
   |
   +-- MISS
        |
        +--> Retrieve relevant passages
        +--> Generate grounded answer
        +--> Cache successful answer + sources (with TTL)
        +--> Return answer + sources
```

---

## 1. Training-Guide Analysis: What Is Implemented vs. Explained

| Training-guide topic | Treatment in this project |
|---|---|
| Application caching: a hit avoids repeated work; a miss follows the normal path | **Implemented.** This is the core assignment requirement. |
| Redis caching and expiration (TTL) | **Implemented.** TTL is configurable through `CACHE_TTL_SECONDS` and must be greater than zero. |
| Cache-unavailable behavior | **Implemented.** The application falls back to the full RAG pipeline and reports `cache_status: "unavailable"`. |
| Freshness and corpus changes | **Partially addressed.** Cache keys include a corpus fingerprint, making old entries unreachable after corpus changes. TTL bounds cache lifetime but does not guarantee freshness by itself. |
| Cache sharing scope | **Implemented as a design decision.** No user ID is included in the cache key because the corpus is shared and non-personal. See §6. |
| Performance measurement | **Implemented.** See `benchmarks/run_benchmark.py`. |
| Cache stampede prevention | **Implemented.** A Redis single-flight lock reduces duplicate rebuilds for concurrent identical requests. See §7. |
| Indexing and query plans | **Explained only.** The application uses an in-memory TF-IDF retrieval index, not a relational database. |
| Read replicas, sharding, CDN, write queues, batching, load shedding, and denormalization | **Explained only.** These are database scaling concepts, not requirements for the minimal RAG caching assignment. |

### Corpus Fidelity

`data/docs/database_scaling_guide.md` contains the substantive text from sections 1–5, including indexing, read scaling, write scaling, caching, failure cases, and conclusions.

It omits:

- The original title block.
- The training objective.
- Figure captions and diagrams.
- Discussion prompts.

Consequently, questions about these omitted elements are not answerable directly from the application's corpus.

---

## 2. Technology Stack and Implementation Deviations

The build environment had no network access and package installation was disabled. `redis-server` was available, but `redis-py`, FastAPI, sentence-transformers, and an LLM SDK were unavailable.

The assignment does not mandate a particular stack, so the following alternatives were used:

| Component | Implementation | Suggested alternative | Impact |
|---|---|---|---|
| Redis | Real Redis Server 7.0.15 | Redis | None |
| Redis client | `app/redis_client.py` — minimal RESP client using standard-library sockets | `redis-py` | Small custom client; `AnswerCache` uses only `get`, `set`, and `delete`, plus locking operations. |
| API | Standard-library `http.server` in `app/api.py` | FastAPI | Provides the required endpoints using lightweight handlers. |
| Embeddings and vector retrieval | scikit-learn TF-IDF (1–2 grams) with cosine similarity, in memory | Hugging Face embeddings | Lower semantic recall. For example, "stampede" may not match "simultaneous rebuilds". |
| Answer generation | Default: `ExtractiveGenerator`. Optional: `OpenAICompatibleGenerator` in `app/llm_http.py` | Hosted or local neural LLM | The default generator extracts relevant sentences rather than generating new text with a neural model. |

### LLM Integration

The optional real-LLM integration supports OpenAI-compatible `/chat/completions` endpoints, including OpenAI, Ollama, vLLM, and LiteLLM configurations.

Enable it using:

```env
LLM_PROVIDER=openai_compatible
```

The client has been tested against a local stub server implementing the same protocol. It has **not been verified against a live hosted or local model**.

The default extractive generator is deterministic and uses retrieved sentences. Answers may occasionally be incomplete or loosely relevant.

### Benchmark Interpretation

The default generator is very fast, so native caching improvements are small in absolute latency.

A separate benchmark scenario uses an explicitly simulated 300 ms generator delay to approximate remote LLM latency. This is an assumption, not a measurement of a real LLM.

---

## 3. Project Structure

```text
app/
  config.py         Settings from environment variables
  redis_client.py   Minimal Redis RESP2 client
  cache.py          AnswerCache: keys, JSON answer/source storage, errors
  chunking.py       Markdown ingestion and chunking
  retriever.py      TF-IDF vectors, cosine retrieval, passage references
  generator.py      Extractive grounded generator
  llm_http.py       Optional OpenAI-compatible LLM generator
  rag.py             RAGService.ask: Redis-first workflow and statistics
  factory.py         Application dependency wiring
  api.py             HTTP API and web UI hosting
  main.py            Application entry point
  static/
    index.html        Web interface
    styles.css        Interface styles
    app.js            Frontend logic

data/
  docs/
    database_scaling_guide.md

tests/                Pytest test suite
benchmarks/
  run_benchmark.py
  results.json
  test_results.txt

scripts/
  start_redis.sh

docker-compose.yml
.env.example
requirements.txt
README.md
```

---

## 4. Installation and Execution

### Requirements

- Python ≥ 3.10
- Docker Compose for the provided Redis service, or Redis 7 running locally
- Dependencies listed in `requirements.txt`

Developed and tested using Python 3.13.18.

### Start Redis

```bash
docker compose up -d redis
```

Alternatively, start Redis 7 on:

```text
127.0.0.1:6379
```

The provided Compose configuration binds Redis to localhost and configures a 256 MB memory limit with `allkeys-lru`.

### Install dependencies

```bash
pip install -r requirements.txt
```

Dependencies include:

- scikit-learn
- numpy
- requests
- pytest

### Start the application

```bash
python -m app.main
```

Open the application in a browser:

```text
http://127.0.0.1:8000
```

### Environment configuration

The application reads configuration from process environment variables. It does not automatically load `.env`.

`.env.example` documents the available settings.

PowerShell example:

```powershell
$env:CACHE_TTL_SECONDS="1800"
python -m app.main
```

### Optional real LLM configuration

PowerShell:

```powershell
$env:LLM_PROVIDER="openai_compatible"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="<model>"
$env:LLM_API_KEY="<key>"

python -m app.main
```

Keep API keys out of source control.

### Demonstrate Cache Miss and Cache Hit

First request:

```bash
curl -s localhost:8000/ask \
  -d '{"question":"What is a read replica?"}'
```

Expected:

```json
{
  "cache_status": "miss"
}
```

Repeat the same request:

```bash
curl -s localhost:8000/ask \
  -d '{"question":"What is a read replica?"}'
```

Expected:

```json
{
  "cache_status": "hit"
}
```

Inspect application statistics:

```bash
curl -s localhost:8000/stats
```

### Run Tests

```bash
python -m pytest -v
```

Requires Redis on `localhost:6379`. Selected tests also use Docker to start isolated Redis instances.

### Run Benchmarks

```bash
python benchmarks/run_benchmark.py
```

Requires Docker. Results are written to:

```text
benchmarks/results.json
```

---

## 5. API Response and Cache Design

### Response Format

```json
{
  "question": "What is a read replica?",
  "answer": "...",
  "sources": [
    {
      "source": "database_scaling_guide.md",
      "chunk_id": "...",
      "heading": "...",
      "score": 0.85,
      "snippet": "..."
    }
  ],
  "answered": true,
  "cache_status": "miss",
  "latency_ms": 1.23
}
```

### Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/ask` | POST | Ask a question and receive an answer with sources. |
| `/stats` | GET | Inspect application statistics. |
| `/health` | GET | Check application health. |

### Cache Status Values

```text
hit
hit_after_wait
miss
miss_not_stored
unavailable
disabled
```

- `hit`: Answer returned directly from Redis.
- `hit_after_wait`: Another concurrent request rebuilt the cache entry while this request waited.
- `miss`: Cache miss followed by successful retrieval, generation, and storage.
- `miss_not_stored`: Cache miss followed by an answer that could not be stored.
- `unavailable`: Redis lookup failed; the application executed the RAG pipeline without a usable cache.
- `disabled`: Caching is disabled.

### Cache Key

The cache key is:

```text
prefix:sha256(normalized_question + context)
```

The context includes:

- Corpus fingerprint.
- `top_k`.
- `min_score`.
- LLM/generator identity.
- Prompt version.

Questions and retrieval/generation configurations therefore do not incorrectly share cache entries.

### Answer and Source Storage

The answer and source references are stored together in a single Redis JSON value:

```json
{
  "q": "normalized question",
  "answer": "generated answer",
  "sources": [],
  "cached_at": 1234567890.0
}
```

This ensures cached answers retain the original source references.

### Successful-Answer Caching

Only successful answers are cached.

Not cached:

- No passages above `min_score`.
- `NoAnswerError`.
- Generator exceptions.
- Empty answers.
- Requests whose Redis lookup failed.

---

## 6. Security and Cache Sharing

Answers derive from a shared, non-personal document corpus.

Therefore, the cache key does not include a user ID. This is a deliberate sharing decision that improves cache reuse without exposing private user-specific documents.

If per-user or per-tenant documents are introduced, a tenant/user identifier must be included in the cache context in `factory.py`. Otherwise, cached answers could be shared across users incorrectly.

Additional security considerations:

- The question text is stored in normalized form in the cache value.
- Do not cache questions containing secrets without adding appropriate encryption or redaction.
- Redis should not be exposed publicly.
- The provided scripts bind Redis to `127.0.0.1`.
- The LLM API key is not included in cache keys, cache values, settings representations, or error messages.
- The cache context contains the model name, preventing answers generated by different configured models from mixing.

---

## 7. Stampede Prevention

**Problem:** An expired popular cache entry can cause multiple concurrent requests to miss together and perform duplicate retrieval and generation.

### Implementation

Implemented in:

- `app/rag.py` → `_acquire_or_wait()`
- `app/cache.py` → `try_lock()` and `release_lock()`

Workflow:

1. After a cache miss, a request attempts:

   ```text
   SET <key>:lock <token> NX EX <lock_ttl>
   ```

2. The winning request rechecks the cache, retrieves passages, generates the answer, stores it, and releases its lock in a `finally` block.

3. Losing requests poll Redis every 50 ms.

4. If the winner creates the cache entry, waiting requests return its answer and sources:

   ```text
   cache_status = hit_after_wait
   ```

5. If the lock holder fails, another request can acquire the lock and rebuild.

6. If the waiting period expires, a waiting request falls back to computing its own answer.

7. If Redis becomes unavailable, lock coordination is skipped.

### Safety

- Locks have TTLs so crashed holders cannot block requests indefinitely.
- Lock release uses an atomic Redis Lua compare-and-delete operation.
- An expired lock acquired by a new owner cannot be deleted by an old owner.
- Failed generation releases the lock.
- Tests verify recovery after lock expiration and failed generation.

### Costs and Limitations

- A cache miss incurs additional Redis round-trips for lock acquisition, rechecking, and release.
- This overhead is visible with the very fast native generator.
- TTL jitter and early/background refresh are not implemented.
- Concurrency tests use threads against one Redis instance.
- Multi-host deployment has not been load-tested.

Stampede protection is an additional reliability feature, not a mandatory assignment requirement.

---

## 8. Test Results

The ZIP includes `benchmarks/test_results.txt`, recording **42 passed** (approximately 13 seconds) for the original archived test suite against a real Redis server.

The current source includes regression tests for malformed source references, heading-only and source-only corpus changes, atomic lock ownership, and lock cleanup when Redis fails during the post-lock cache recheck.

The user-reported Windows run (Python 3.13.12) collected and passed **74 tests in 27.03 seconds before the fixes in this revision**. This revision adds five focused regression tests, bringing the expected collection to 79. The archived `benchmarks/test_results.txt` remains historical evidence for the earlier 42-test run. Re-run the current suite after applying this revision:

```bash
python -m pytest -v
```

### Mandatory Workflow Tests

| # | Scenario | Test |
|---|---|---|
| 1 | First question produces a miss | `test_01_first_question_is_cache_miss` |
| 2 | Repeated question produces a hit | `test_02_repeated_question_is_cache_hit` |
| 3 | Cached answer preserves sources | `test_03_cached_answer_keeps_correct_sources` |
| 4 | Hit skips retrieval | `test_04_cache_hit_does_not_retrieve` |
| 5 | Hit skips generation | `test_05_cache_hit_does_not_generate` |
| 6 | Miss performs retrieval and generation | `test_06_cache_miss_runs_retrieval_and_generation` |
| 7 | Only successful answers are cached | `test_07a`, `test_07b`, `test_07c` |
| 8 | Expiration follows configured TTL | `test_08_expiration_follows_configured_ttl`, `test_08b` |
| 9 | Redis failure and corrupt entries | `test_09a`, `test_09b`, `test_09c` |
| 10 | Different questions do not share entries | `test_10_different_questions_do_not_share_entries` |
| 11 | Redis is checked before retrieval | `test_order_redis_is_checked_before_retrieval` |

### Additional Reliability Tests

| Area | Coverage |
|---|---|
| Stampede protection | Concurrent identical misses; different questions do not block each other. |
| Lock recovery | Failed generation, lock expiration, and waiting timeout. |
| LLM integration | Successful response, cache hit skips HTTP, failures are not cached, unreachable provider, empty responses, and invalid responses. |
| API key protection | API key is absent from cache keys and cached values. |
| HTTP hardening | Negative/non-numeric content length, oversized bodies, malformed JSON, and Unicode questions. |
| Source validation | Malformed cached source references are rejected before serving. |
| Corpus fingerprint | Heading-only corpus changes invalidate old cache entries. |
| Lock ownership | Atomic lock release protects new owners from old-owner deletion. |

---

## 9. Benchmark Results

**Source:** `benchmarks/results.json`

### Environment

- Python 3.13.18.
- Linux x86_64.
- Redis Server 7.0.15.
- Redis on the same host over loopback TCP.
- 18 document chunks.

### Workload

10 distinct questions × 10 repetitions = 100 requests, using a seeded shuffle.

- Scenario A: Cache disabled.
- Scenario B: Cache enabled, cold start.

### Results

| Run | Scenario | Avg latency | p95 | Hit rate | Retrieval ops | Generation ops |
|---|---|---:|---:|---:|---:|---:|
| Native generator | No cache | 0.669 ms | 0.751 ms | — | 100 | 100 |
| Native generator | Cache | 0.253 ms | 1.688 ms | 90% | 10 | 10 |
| Simulated 300 ms LLM | No cache | 301.738 ms | 302.099 ms | — | 100 | 100 |
| Simulated 300 ms LLM | Cache | 30.317 ms | 302.341 ms | 90% | 10 | 10 |

### Interpretation

**Native generator:**

- 2.64× faster average latency.
- 62.2% reduction in average latency.
- Average hit latency: 0.089 ms.
- Average miss latency: 1.728 ms.

**Simulated LLM:**

- 9.95× faster average latency.
- 90.0% reduction in average latency.
- Average hit latency: 0.078 ms.
- Average miss latency: approximately 302 ms.

In both scenarios, caching avoided 90 retrieval operations and 90 generation operations.

### Limitations

- Single machine.
- Redis on loopback, without a real network hop.
- Small corpus.
- Only 100 requests.
- Single run per configuration.
- No confidence intervals.
- The 300 ms LLM delay is simulated.
- Stampede locking is tested for correctness but not load-benchmarked.

The benchmark's 90% hit rate is determined by the workload construction and should not be interpreted as an expected real-world hit rate.

---

## 10. Assignment Compliance Checklist

| # | Requirement | Status | Implementation |
|---|---|---|---|
| 1 | Redis checked before retrieval/generation | PASS | `rag.py` → `RAGService.ask()` |
| 2 | Cache key generated per question/context | PASS | `cache.py` → `AnswerCache.make_key()` |
| 3 | Hit returns cached answer and sources | PASS | `rag.py` → `ask()` |
| 4 | Hit skips retrieval and generation | PASS | `rag.py` → `ask()` |
| 5 | Miss retrieves relevant passages | PASS | `retriever.py` → `retrieve()` |
| 6 | Miss generates grounded answer | PASS | `generator.py` → `generate()` |
| 7 | Successful answer is cached | PASS | `cache.py` → `AnswerCache.set()` |
| 8 | Sources are cached with their answer | PASS | Single Redis JSON value |
| 9 | Only successful answers are cached | PASS | `rag.py` → `ask()` / `_unsuccessful()` |
| 10 | Configurable cache expiration | PASS | `config.py`, `cache.py` |
| 11 | Redis failures handled safely | PASS | `cache.py`, `rag.py`, `redis_client.py` |
| 12 | Different questions/configurations do not collide | PASS | `cache.py`, `factory.py` |
| 13 | Source references validated before serving | PASS | Cache validation in `cache.py` / `rag.py` |
| 14 | Document ingestion and retrieval | PASS | `chunking.py`, `retriever.py` |
| 15 | Modular architecture | PASS | `app/` structure |
| 16 | Performance benchmark | PASS | `benchmarks/run_benchmark.py` |
| 17 | Automated tests | NOT VERIFIED AFTER PATCH | User-reported pre-patch run: 74 passed; this revision adds three regression tests |
| 18 | Training-guide analysis | PASS | README §1 |
| 19 | Stampede prevention | PASS | `rag.py`, `cache.py` |
| 20 | Docker Compose configuration | PASS | `docker-compose.yml` |
| 21 | Live hosted/local LLM | NOT VERIFIED | `llm_http.py` |
| 22 | Neural embeddings | NOT IMPLEMENTED | TF-IDF is used instead |
| 23 | Hybrid search / reranking | NOT IMPLEMENTED | Optional, not required |

\* The 74-pass result is from the user's reported local run before the fixes in this ZIP. Re-run the full suite after applying these fixes; the archived 42-pass report is historical evidence only.

### Mandatory Assignment Summary

| Assignment requirement | Compliance |
|---|---|
| Functional RAG application | PASS |
| Answers with source references | PASS |
| Redis checked before retrieval/generation | PASS |
| Cache-hit behavior | PASS |
| Cache-miss behavior | PASS |
| Successful answer + sources cached | PASS |

The project implements the required Redis caching workflow. Optional extensions such as hybrid search and reranking are intentionally excluded.

---

## Final Conclusion

This project demonstrates a minimal RAG application enhanced with Redis answer caching.

The implementation prioritizes:

- Correct Redis-first request handling.
- Efficient cache hits.
- Grounded answers with source references.
- Successful-answer caching.
- Configurable expiration.
- Cache failure handling.
- Concurrent request coordination.
- Measurable retrieval and generation savings.

The application is intentionally lightweight and avoids unnecessary infrastructure.

**The core assignment is implemented.** Remaining verification consists of running the expanded tests, validating the Docker Compose setup, and optionally testing the real LLM integration against an actual model.
