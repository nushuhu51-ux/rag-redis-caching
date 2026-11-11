# Database Performance and Scaling

## System at a glance
Imagine a social application where people publish posts and view posts from other users. When someone publishes a post, the client sends it to the application server, which validates the request and writes it to the database. When someone views posts, the application reads stored records and returns them.
A write changes data; a read retrieves data. Both pass through the application and use shared resources. As usage grows, ask which part is under pressure and what behavior users expect.

## Database access patterns
An access pattern is how an application reads or changes data. Our social application might create a post, retrieve one post by ID, list a user's recent posts, and record a like.
These operations do different work. Before choosing a database or optimization, identify the data, the queries, the expected volume, and the required correctness. Begin with a database that meets the needs; add components for a clear reason.

## Indexing overview
Problem: As the posts table grows, finding one user's recent posts can require the database to examine a lot of records.
What it is: An index is an extra data structure that gives the database an alternative way to locate records. A B-tree maintains ordered keys and can support equality, range, and some ordering queries.
Example: An index on (user_id, created_at) can support looking up a user's posts in time order. Whether the database chooses it depends on the actual query, data and query plan.
Trade-off: An index uses storage and needs maintenance when relevant rows change. It can make a read cheaper while adding work to writes. Engineers choose indexes; the database maintains and uses them when appropriate.

## Scaling reads: the problem
A popular post is written once but read many times. Increasing reads compete for database resources. The goal is to handle that workload while meeting response-time and freshness needs.

## Scaling reads: optimize within your database
Indexes: Match indexes to frequent filters and ordering. Check the query plan to confirm they reduce work; each index also adds storage and write maintenance.
Vertical scaling: Add CPU, memory or faster storage for a measured resource bottleneck. One machine still has a capacity limit.
Denormalization: Store selected duplicated values or precomputed results to reduce joins and repeated computation. Define how those values stay current.

## Scaling reads: scale your database horizontally
Read replicas: Copy changes from the primary and route suitable reads to replicas. This spreads read traffic but does not increase the primary's write capacity.
Freshness: Replicas can lag. Reads that must immediately see a completed write need an appropriate consistency guarantee or routing policy.
Sharding: Split records across nodes using a shard key. Queries that target one shard scale more easily than queries that must combine results from many shards.

## Scaling reads: add external caching layers
Application cache: Reuse frequently requested data or results. A usable hit avoids repeated database work; a miss follows the normal data path.
CDN: Serve suitable shared content from locations closer to users, reducing origin traffic and network travel.
Trade-offs: Decide acceptable freshness, access scope and behavior when the cache is unavailable. Detailed write strategies and eviction policies remain the reading assignment.

## When read-scaling approaches help
An index can help find recent posts, a cache can reuse a popular post, and replicas can distribute reads that tolerate lag. If one database already meets the workload, extra components may be unnecessary.

## Read scaling failure cases and design challenges
Queries slow down: Inspect the query plan and indexes. A hot cached item can overload a cache node. An expired popular entry can trigger simultaneous rebuilds; coordination or early refresh can reduce duplicate work. For immediately visible updates, choose a read path and consistency approach that satisfy the requirement; TTL alone is not a guarantee.
Conclusion: Make each read cheaper, reuse suitable results, and distribute remaining work based on measured load and required freshness.

## Scaling writes: the challenge
Thousands of users like one popular post. Each event creates work, and updates to the same counter can compete. The goal is manageable write load while preserving correctness and durability.

## Scaling writes: vertical scaling and write optimization
Vertical scaling: Add CPU, memory or faster storage when measurements show that resource is limiting writes. Hardware upgrades have a practical ceiling.
Write optimization: Review unnecessary indexes, expensive queries and lock contention. Preserve the constraints and durability guarantees the application needs.
Database choice: Match the storage engine and data model to the write pattern. A system suited to append-only events may differ from one needed for coordinated transactional updates.

## Scaling writes: sharding and partitioning
Horizontal sharding: Distribute records across nodes using a shard key. Choose a key that spreads traffic and supports common queries; a single hot record can still overload one shard.
Vertical partitioning: Separate data with different access patterns, such as post content and frequently updated metrics. Retrieving a complete view may require combining data later.
Trade-offs: Routing, rebalancing and operations across shards become more complex. Splitting data does not automatically make coordinated updates safe.

## Scaling writes: queues and load shedding
Write queues: Buffer suitable temporary bursts so workers process work at a sustainable rate. Queued acceptance is different from a completed database write.
Recovery: Retried or redelivered work must not apply the same change twice. Monitor backlog and processing delay; a queue cannot solve sustained overload.
Load shedding: Reject or discard selected work only when requirements permit, with clear client behavior. An occasional location update and a payment have different consequences.

## Scaling writes: batching and hierarchical aggregation
Batching: Send multiple operations together to reduce per-operation overhead. Larger batches can improve throughput but increase waiting time.
Aggregation: Combine suitable updates, such as several increments into one counter update. Hierarchical aggregation combines partial results in stages to spread the work.
Trade-offs: Results may appear later. Recovery must account for duplicate events and buffered work so retries do not corrupt counts or lose accepted updates.

## Scaling writes: when these approaches help, and failure cases
Distribute writes to different posts, buffer a temporary burst, or combine counts when delay is acceptable. If one database already meets the workload and reliability goals, extra components may be unnecessary.
Resharding requires routing, safe cutover, retries and reconciliation; dual writes alone do not guarantee no data loss. A hot key can overload one shard. Splitting every suitable counter is simpler but increases storage and read work; splitting only hot counters saves resources but requires coordination.
Conclusion: Identify the write bottleneck, then distribute, buffer or combine work only when the application can accept the resulting complexity and behavior.
