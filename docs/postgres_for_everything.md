# Reference: "Just Use Postgres for Everything" — claims and validity

**Source:** <https://www.amazingcto.com/postgres-for-everything/> — Stephan Schmidt (Amazing CTO).
Originally a 2022 essay ([HN discussion](https://news.ycombinator.com/item?id=33934139), ~1100
points); the page now shows "Updated December 13, 2025" and has grown from ~9 to 25 points.
Community companion list: <https://github.com/Olshansk/postgres_for_everything>.

**Thesis:** consolidate the stack onto Postgres to reduce moving parts, cognitive load, and
operational overhead ("one connection pool, one monitoring dashboard, one backup strategy");
Postgres can carry an app "up to millions of users"; three 99.9 % systems compose to ~99.7 %.
Add specialized tools only when you actually hit Postgres limits — not when you think you will.

**Why this doc exists:** this project adopted Postgres for stage decoupling (work queue +
multi-process writes — see `stage_contracts.md`). The
article is the broader "how far can we ride Postgres" map. Each point below is the article's
claim, followed by a researched verdict: **Solid** / **Solid-with-caveats** / **Contested**.

## The 25 points

1. **Caching instead of Redis** — UNLOGGED tables + TEXT/JSON with stored-proc expiry.
   **Solid-with-caveats.** UNLOGGED skips WAL, helping writes, not reads; head-to-head benchmarks
   put Redis at ~900k req/s vs Postgres ~15k TPS on the same box
   ([dizzy.zone](https://dizzy.zone/2025/09/24/Redis-is-fast-Ill-cache-in-Postgres/),
   [De Lio](https://dev.to/raphaeldelio/can-postgres-replace-redis-as-a-cache-2ne1),
   [Cybertec](https://www.cybertec-postgresql.com/en/postgresql-vs-redis-vs-memcached-performance/)).
   Fine at modest cache QPS; wrong for sub-ms latency or six-figure ops/sec. UNLOGGED tables are
   truncated on crash recovery (acceptable for a cache).

2. **Message queue instead of Kafka** — `FOR UPDATE SKIP LOCKED` "if you only need a message
   queue"; River (Go) as a job queue.
   **Solid-with-caveats — and the load-bearing point for this project.** The canonical Postgres
   queue pattern; a well-tuned cluster handles tens of thousands of jobs/sec
   ([Netdata](https://www.netdata.cloud/academy/update-skip-locked/),
   [vrajat](https://vrajat.com/posts/postgres-queue-skip-locked-unlocked/)). Failure modes: with
   thousands of workers each claim scans past locked rows (CPU burn —
   [pgsql-hackers](https://postgrespro.com/list/thread-id/2505440)); high-churn job rows cause
   dead-tuple bloat needing aggressive autovacuum/partitioning
   ([PlanetScale](https://planetscale.com/blog/keeping-a-postgres-queue-healthy),
   [richyen](https://richyen.com/postgres/2026/05/04/postgres_job_queue.html)). Kafka remains right
   for log/stream semantics at millions of events/sec. Python equivalents of River: Procrastinate,
   PGQueuer.

3. **Data warehouse** — Postgres with TimescaleDB; DuckDB for S3.
   **Contested.** Timescale is excellent for time-series + point lookups, but ClickHouse is
   consistently 6–7× faster on large aggregations with far better compression
   ([Tinybird](https://www.tinybird.co/blog/clickhouse-vs-timescaledb),
   [oneuptime](https://oneuptime.com/blog/post/2026-01-21-clickhouse-vs-timescaledb/view));
   Timescale's TSL license also restricts managed-service use. Fine to ~hundreds of GB; not a
   serious OLAP warehouse.

4. **Data lake** — DuckDB/DuckLake with Postgres as the catalog.
   **Solid-with-caveats (young tech).** DuckLake (May 2025) really does use Postgres as a lakehouse
   catalog, and pg_duckdb hit 1.0 ([MotherDuck](https://motherduck.com/blog/pg-duckdb-release/),
   [pg_ducklake](https://github.com/relytcloud/pg_ducklake)) — but the space is <2 years old vs the
   Iceberg/Spark ecosystem. An accurate "possible," not a battle-tested default.

5. **In-memory OLAP** — pg_analytics with Apache DataFusion.
   **Contested — the named tool is dead.** ParadeDB's
   [pg_analytics is archived](https://github.com/paradedb/pg_analytics); the living alternative is
   [pg_duckdb](https://github.com/duckdb/pg_duckdb) (10–100× analytic speedups). The idea survives;
   the article's specific recommendation is stale.

6. **Document store instead of MongoDB** — JSONB; DocumentDB as drop-in.
   **Solid-with-caveats.** JSONB + GIN covers most "Mongo because JSON" cases
   ([Ivon](https://medium.com/@yurexus/can-postgresql-with-its-jsonb-column-type-replace-mongodb-30dc7feffaf3)).
   The real limit is **TOAST**: documents over ~2 KB get compressed/chunked out-of-row, causing read
   amplification and degraded partial updates
   ([Pachot](https://dev.to/franckpachot/postgresql-jsonb-size-limits-to-prevent-toast-slicing-9e8),
   [MongoDB benchmark](https://www.mongodb.com/company/blog/technical/evaluation-update-heavy-workloads-postgresql-jsonb-and-bson) — vendor, but the mechanism is real).
   The DocumentDB reference is current: Microsoft's MIT extension, donated to the Linux Foundation
   Aug 2025, powers FerretDB 2.x
   ([Microsoft](https://opensource.microsoft.com/blog/2025/08/25/documentdb-joins-the-linux-foundation/)).

7. **Cron daemon** — pg_cron.
   **Solid.** Mature (Citus), offered on RDS/Aurora/Cloud SQL/Supabase
   ([repo](https://github.com/citusdata/pg_cron)). Caveats: ≤32 concurrent jobs, each holds a
   connection; a job never overlaps itself; `cron.job_run_details` needs its own cleanup; 1-second
   granularity floor.

8. **Geospatial** — PostGIS.
   **Solid — the strongest claim in the list.** [PostGIS](https://postgis.net/) is the industry
   standard (OpenStreetMap rendering, Carto); generally better than dedicated alternatives.

9. **Full-text search instead of Elasticsearch** — tsvector/GIN, `websearch_to_tsquery`.
   **Solid-with-caveats.** Handles "the first 80 %" to a few million rows / low-hundreds QPS
   ([Xata](https://xata.io/blog/postgres-full-text-search-postgres-vs-elasticsearch),
   [msezer.dev](https://msezer.dev/articles/postgresql-full-text-search)). Missing vs Elastic:
   BM25 relevance, faceting, typo tolerance, horizontal scale; degrades at tens of millions of
   rows. [ParadeDB pg_search](https://www.paradedb.com/blog/elasticsearch-vs-postgres) adds BM25
   in-Postgres as a middle path.

10. **JSON API generation** — `row_to_json`/`jsonb_agg`, PostgREST.
    **Solid-with-caveats.** PostgREST is proven (backbone of Supabase —
    [postgrest.org](https://postgrest.org/)). The caveat is architectural taste: business logic
    migrates into SQL/views, which many teams find hard to test and version.

11. **Auditing** — pgaudit.
    **Solid.** [pgaudit](https://github.com/pgaudit/pgaudit) is the compliance standard on all
    major clouds. READ logging can explode log volume; configured conservatively the overhead is
    <~5 % ([Neon](https://neon.com/blog/postgres-logging-vs-pgaudit)). Trigger-based history tables
    complement it for row-level data audit.

12. **GraphQL** — Hasura / PostGraphile / pg_graphql.
    **Solid.** All production-grade; Hasura compiles GraphQL to single SQL queries (no N+1 —
    [discussion](https://news.ycombinator.com/item?id=27550151)). Same logic-in-DB tradeoff as #10.

13. **Vector database** — pgvector.
    **Solid-with-caveats.** With HNSW it matches or beats Qdrant/Pinecone at ~1M-vector scale on
    equal compute, and is the right default when vectors live next to relational data
    ([HuggingFace benchmark](https://huggingface.co/blog/ImranzamanML/pgvector-vs-elasticsearch-vs-qdrant-vs-pinecone-vs),
    [Tiger Data](https://www.tigerdata.com/blog/pgvector-vs-qdrant)). Breaks down ~5–10M+ high-dim
    vectors (HNSW for 10M × 1536-dim needs 60–70 GB RAM; dedicated engines win at 100M+ —
    [tensoria](https://tensoria.fr/en/blog/vector-database-comparison)).

14. **Session store** — indexed session table; hstore as KV.
    **Solid-with-caveats.** Standard pattern (Django/Rails ship DB session backends); high-churn
    rows add vacuum pressure, and Redis-class QPS doesn't transfer (see #1). The **hstore**
    suggestion is dated — string-only, superseded by JSONB
    ([docs](https://www.postgresql.org/docs/current/hstore.html)).

15. **Rate limiting** — atomic updates + time windows instead of Redis counters.
    **Solid-with-caveats.** Works in production to roughly 10–15k req/s/node with sub-ms p50
    ([token-bucket writeup](https://dev.to/yugabyte/rate-limiting-with-postgresql-yugabytedb-token-buckets-function-5dh8),
    [Go+Postgres report](https://medium.com/@the_atomic_architect/how-i-built-a-rate-limiter-that-actually-works-in-production-using-go-and-postgresql-f0769ef9bf76)).
    Past that, hot-bucket contention and pool pressure favor Redis.

16. **Distributed locks** — advisory locks.
    **Solid-with-caveats.** Great single-cluster coordination primitive. The big trap:
    **session-level advisory locks break under PgBouncer transaction pooling** (lock taken on one
    backend, "released" on another) — use `pg_advisory_xact_lock` variants or session-mode pooling
    ([good_job issue](https://github.com/bensheldon/good_job/issues/52),
    [Crunchy Data](https://www.crunchydata.com/blog/postgres-locking-when-is-it-concerning)).
    Locks vanish with the connection (usually a feature); not a cross-cluster lock.

17. **Event sourcing** — ordered, atomic event storage.
    **Solid-with-caveats.** Production-proven (Marten — <https://martendb.io/events/>; Commanded —
    <https://github.com/commanded/eventstore>); fits well under ~tens of thousands of events/sec
    with partitioning ([reference impl](https://github.com/eugene-khyst/postgresql-event-sourcing)).
    Gapless global ordering under concurrent writers is genuinely tricky (sequence gaps /
    commit-order races).

18. **Testing database** — transaction rollback per test; template databases.
    **Solid.** Both are standard, documented practice
    ([template databases](https://www.postgresql.org/docs/current/manage-ag-templatedbs.html);
    pytest-django/Rails wrap tests in transactions). Rollback-per-test can't exercise code that
    itself commits or uses multiple connections — relevant here, which is why this repo uses
    per-test ephemeral schemas instead (see `tests/conftest.py`).

19. **Metrics/monitoring** — pg_stat_statements "for application performance metrics."
    **Solid-with-caveats.** The canonical low-overhead query-performance view
    ([docs](https://www.postgresql.org/docs/current/pgstatstatements.html)) — but it monitors the
    *database*, not the application; it is not an APM/tracing replacement.

20. **Webhooks** — HTTP extensions sending notifications on data changes.
    **Contested.** Synchronous [pgsql-http](https://github.com/pramsey/pgsql-http) **blocks the
    transaction** during the HTTP call — dangerous in triggers. Async
    [pg_net](https://github.com/supabase/pg_net) fixes blocking but has documented dropped-request
    issues under load and no retry guarantees
    ([Sequin benchmark](https://blog.sequinstream.com/benchmarking-pg_net-part-1/)). The robust
    pattern is an outbox table + worker — i.e. point #2, not in-DB HTTP.

21. **File storage** — metadata and large objects in-DB.
    **Solid for metadata, Contested for the bytes.** In-DB blobs read ~10× slower than the
    filesystem, bloat pg_dump (a 7.5 GB bytea table → 12 h dump reported), large objects need
    manual `vacuumlo`, bytea caps at 1 GB
    ([Cybertec](https://www.cybertec-postgresql.com/en/binary-data-performance-in-postgresql/),
    [EDB](https://www.enterprisedb.com/blog/those-darn-large-objects)). Consensus: metadata in
    Postgres, bytes in S3/filesystem.

22. **Cryptography** — pgcrypto.
    **Solid-with-caveats.** Fine for column encryption / password hashing
    ([docs](https://www.postgresql.org/docs/current/pgcrypto.html)). Skipped caveats: keys passed
    in SQL can leak into logs and pg_stat_statements; in-DB crypto means the DB sees plaintext and
    keys (serious threat models encrypt application-side); bcrypt-in-DB burns DB CPU.

23. **Scaling** — pg_partman.
    **Solid-with-caveats.** [pg_partman](https://github.com/pgpartman/pg_partman) automates
    time/serial partitioning — right for big append-heavy tables (and for pruning queue/event
    tables). But partitioning is *single-node* scaling; the honest bullet would also mention read
    replicas and Citus. "Millions of users on one big Postgres" is nonetheless well-attested.

24. **Multi-tenancy** — row-level security.
    **Solid-with-caveats.** RLS is real isolation, used in AWS's reference SaaS architectures
    ([AWS](https://aws.amazon.com/blogs/database/multi-tenant-data-isolation-with-postgresql-row-level-security/)).
    Pitfalls: missing `(tenant_id, …)` composite indexes (~100× slowdowns), per-row policy cost,
    tenant-context leakage through pools, superuser/BYPASSRLS holes
    ([Bytebase](https://www.bytebase.com/blog/postgres-row-level-security-limitations-and-alternatives/)).

25. **Scheduled jobs beyond cron** — pg_timetable.
    **Solid.** [pg_timetable](https://github.com/cybertec-postgresql/pg_timetable) (Cybertec)
    supports chains, concurrency control, shell tasks. It's an external Go binary, so "one more
    process" — slightly against the article's own one-system thesis.

## What the article omits: LISTEN/NOTIFY

The article (wisely) never recommends LISTEN/NOTIFY, but it's the most famous failure mode in this
genre: `NOTIFY` serializes **all** committing transactions through a global lock under concurrent
writers — see the [recall.ai outage writeup](https://www.recall.ai/blog/postgres-listen-notify-does-not-scale).
At this project's concurrency it would be fine, but polling with SKIP LOCKED is simpler and has no
such edge. Don't add LISTEN/NOTIFY to wake workers.

## Overall assessment

The thesis — default to Postgres, add specialized systems only at *measured* limits — is
mainstream-validated for small teams. The 2025 list is uneven, though: rock-solid (PostGIS,
pg_cron, SKIP LOCKED, pgaudit, JSONB, testing patterns, pgvector at moderate scale), stale
(pg_analytics archived, hstore legacy), and genuinely contested (Timescale-as-warehouse, in-DB
webhooks, blobs in the DB). The recurring HN criticism
([2022](https://news.ycombinator.com/item?id=33934139),
[2024](https://news.ycombinator.com/item?id=41272854)) is that Postgres-for-X is often a worse
*tool* but a better *stack* — which is exactly the article's bet, and a correct one below roughly
tens-of-thousands-of-ops/sec per concern.

## Community companion list (Olshansk/postgres_for_everything)

<https://github.com/Olshansk/postgres_for_everything> — ~2.3k stars, actively maintained
(last push 2026-04), PR-driven. A flat link aggregation (~30 categories, no quality bar or
benchmarks) inspired by the essay and the
[cpursley gist](https://gist.github.com/cpursley/c8fb81fe8a7e5df038158bdfe0f06dbb). Treat it as a
discovery index, not an endorsement list. It extends rather than contradicts the essay — and its
dedicated pooling/sharding sections (pgbouncer, supavisor, pgdog, spqr) implicitly concede the
scale limits the essay glosses over.

Entries most relevant to this pipeline:

- **Queues:** [pgqueuer](https://github.com/janbjorge/pgqueuer) — the only *Python* job-queue lib
  in the list (asyncio, SKIP LOCKED + LISTEN/NOTIFY); the closest off-the-shelf replacement for our
  hand-rolled queue if it ever grows beyond us. [pgmq](https://github.com/tembo-io/pgmq) — SQS
  semantics as a PG *extension* with a Python client (visibility timeouts, archival).
  [pgque](https://github.com/NikolayS/pgque) — one SQL file + pg_cron, philosophically closest to
  what we built. The list's [2ndQuadrant SKIP LOCKED explainer](https://www.2ndquadrant.com/en/blog/what-is-select-skip-locked-for-in-postgresql-9-5/)
  is our pattern's canonical reference, and
  [Choose Postgres queue technology](https://adriano.fyi/posts/2023-09-24-choose-postgres-queue-technology/)
  is the strongest single argument for plain-Postgres queues over Redis/RabbitMQ/SQS.
  (Notably absent from the list: Procrastinate, the best-known psycopg3-native Python queue.)
- **Worker wake-up:** brandur's [Notifier Pattern](https://brandur.org/notifier) essay — the
  careful way to use NOTIFY if we ever want push instead of polling; see the LISTEN/NOTIFY caution
  above before considering it.
- **Observability:** nothing queue-specific exists; [pghero](https://github.com/ankane/pghero)
  (perf dashboard), pgmonitor (Prometheus/Grafana), and pgcli/pgweb for ad-hoc inspection.
- **Audit/CDC (if stage-write audit is ever wanted):**
  [supa_audit](https://github.com/supabase/supa_audit) or [bemi](https://github.com/BemiHQ/bemi)
  lightweight; [pgstream](https://github.com/xataio/pgstream)/Sequin to stream changes out.
- **FTS:** the list adds [pg_search](https://github.com/paradedb/paradedb) (BM25) as the upgrade
  path, plus a [VectorChord "FTS slow myth" rebuttal](https://blog.vectorchord.ai/postgresql-full-text-search-fast-when-done-right-debunking-the-slow-myth)
  supporting native FTS done right.
- **Testing:** [pgtestdb](https://github.com/peterldowns/pgtestdb) (Go) — the fast per-test-DB
  pattern our pytest fixtures mirror with per-test schemas.
- **HTTP-from-Postgres** (pgsql-http/pg_net): exists, deliberately not used here — fingerprint-heavy
  scraping (curl_cffi/pydoll) cannot run from inside Postgres.

## What applies to this project

- **#2 (SKIP LOCKED queue) — adopted** for the `jobs` table (see `stage_contracts.md`). Hygiene to
  keep: partial index on the live-status claim predicate, short claim transactions, prune/archive
  done rows (or pg_partman, #23) as `scrape-menus`' daily per-store job churn rises (the
  Scrapy menu stage that once gated this was superseded).
- **#16 (advisory locks)** — viable alternative for "one process per state" guards; remember the
  PgBouncer transaction-pooling trap if a pooler ever appears. We chose queue claims for uniformity.
- **#6 (JSONB)** — right home for raw scrape payloads (Stage-3 menus): extract hot query fields
  into real columns, keep the raw blob as JSONB archive; mind TOAST above ~2 KB on hot paths.
- **#9 (FTS)** — at dispensary/product-catalog scale, Postgres FTS with `websearch_to_tsquery` +
  pg_trgm is entirely sufficient; Elasticsearch is not justified.
- **#18 (testing)** — this repo uses per-test ephemeral schemas (not rollback-per-test) because the
  code under test commits.
- **#1/#14 (cache/session)** — supports the "don't add Redis" instinct at scraper request rates.
- **Multi-process writes generally** — Postgres's bread and butter (MVCC); just mind connection
  count (one per worker; pool if workers ≫ ~50).
