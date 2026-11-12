**Caching concepts, RAG, vector databases, and application design**

***Prepared by Samuel Teshale***

# 1. RAG and Vector Database Background

Retrieval-Augmented Generation (RAG) improves an LLM’s answers by first
retrieving relevant information from an external knowledge source and
then providing that context to the model to generate a grounded
response.

A typical RAG workflow is:

- **Document preparation:** Documents are split into smaller pieces
  called chunks.

- **Embedding:** Each chunk is converted into a numerical representation
  called an embedding.

- **Storage:** The embeddings are stored in a vector database.

- **Question processing:** A user’s question is also converted into an
  embedding.

- **Retrieval:** Similarity search finds the chunks most relevant to the
  question.

- **Generation:** The retrieved chunks are supplied to the LLM with the
  question so it can generate a grounded answer.

Important distinction: A vector database stores and searches vectors
(often with metadata and source references). It is not the same as a
response cache. In this application, Redis stores completed answers and
their sources so the system can avoid repeating retrieval and generation
for identical questions.

# 2. Cache-Aside vs. Read-Through

| Approach | How it works | Main benefit / trade-off |
|----|----|----|
| Cache-aside | The application checks the cache. On a miss, it reads the underlying data source, returns the result, and explicitly writes the result to the cache. | Flexible because the application controls loading, but the application must implement the cache logic. |
| Read-through | The application asks the cache for data. On a miss, the cache invokes a configured loader to fetch and store the data. | Simplifies application code, but requires cache-loader support and places loading responsibility in the cache layer. |

# 3. Cache Write Policies

| Policy | How it works | Benefit / trade-off |
|----|----|----|
| Write-through | Updates the cache and database together. | Improves consistency, but adds write latency. |
| Write-behind / write-back | Updates the cache first and writes to the database asynchronously. | Offers fast writes, but recent updates may be lost if the cache fails before flushing. |
| Write-around | Writes directly to the database and bypasses the cache. | Avoids filling the cache with rarely read data, but the next read may miss and load the value. |

# 4. TTL, Invalidation, and Eviction

- **TTL (Time to Live):** Sets how long a cache entry may remain before
  it expires.

- **Invalidation:** Deliberately removes or refreshes an entry when its
  underlying data or relevant configuration changes, helping prevent
  stale results.

- **Eviction:** Removes entries to free memory when the cache reaches
  its memory limit.

**Key difference:** TTL is time-based expiry; eviction is
capacity-driven removal.

# 5. Eviction Policies and Redis

| Policy | What it removes | What it favors / limitation |
|----|----|----|
| LRU (Least Recently Used) | Entries that have not been accessed recently. | Favors recent activity. |
| LFU (Least Frequently Used) | Entries with the lowest access frequency. | Favors repeatedly popular items. |
| FIFO (First In, First Out) | The oldest inserted entry, regardless of use. | Simple, but ignores access patterns. |
| Random | An arbitrary entry. | Low selection overhead, but unpredictable. |

Redis supports approximate LRU and LFU, random eviction, and
TTL-oriented volatile policies. Its maxmemory policies include:

- allkeys-lru

- allkeys-lfu

- allkeys-random

- volatile-lru

- volatile-lfu

- volatile-random

- volatile-ttl

- noeviction — rejects writes that require more memory.

Redis does not offer standard FIFO as a dedicated maxmemory eviction
policy.

# 6. Choices in My RAG Application: Benefits and Trade-offs

My application uses Redis with the cache-aside pattern for read/query
caching. The cache is checked before document retrieval or LLM
generation.

## 6.1 Request flow

- **Check Redis first:** Look for a cached result before starting
  retrieval or generation.

- **Cache hit:** Return the saved answer and source references. Skip
  both document retrieval and LLM generation.

- **Cache miss:** Retrieve relevant document chunks, including through
  vector similarity search.

- **Generate:** Ask the LLM to produce an answer grounded in the
  retrieved chunks.

- **Cache the successful result:** Store the answer and its source
  references together.

## 6.2 Cache quality and correctness

- **Do not cache unsuccessful results:** Failed, empty, or irrelevant
  answers should not be cached.

- **Use a positive TTL:** Limit how long a cached answer remains
  available.

- **Use appropriate cache keys:** Keys should distinguish normalized
  questions, relevant context, corpus/document versions, and generation
  configuration so different inputs do not incorrectly share results.

- **Handle Redis failure gracefully:** If Redis is unavailable, the
  application should still produce a fresh answer rather than fail
  solely because caching failed.

## 6.3 Eviction choice

LRU is a sensible memory policy when recent queries are likely to recur.
LFU may be better when a smaller set of questions is consistently
popular.

## 6.4 Benefits and trade-offs

Benefits

- Reduces response latency.

- Avoids repeated vector searches.

- Reduces repeated LLM usage and associated cost.

Trade-offs

- Answers may become stale until TTL expiry or invalidation.

- Redis introduces memory requirements and operational overhead.

- Different cache keys can cause cache misses, even for similar
  questions.
