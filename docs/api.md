# API reference (the engine surface)

The public surface a pipeline or plugin author calls. Signatures are the source of truth
([`../rung/`](../rung)); this is the map. Everything is `psycopg`-based synchronous SQL except the
access-method runners, which are `async`.

## `rung.db` — connection + schema

```python
db.get_connection() -> DBConn                 # reads DATABASE_URL (default: local dev Postgres)
db.create_tables(conn) -> None                # create the infra tables (idempotent); commits
db.one(conn, sql, params=()) -> tuple         # fetch a single row
DBConn                                         # = psycopg.Connection; every signature takes one
```

`create_tables` currently also creates the reference application's tables; a planned
`create_engine_tables()` will build only the generic infra. Your pipeline creates and writes its own
tables directly.

## `rung.access` — the cost-ranked ladder

```python
@dataclass(frozen=True)
class AccessMethod:
    name: str
    cost_rank: int            # try-priority: lower is tried first
    run: MethodRunner

# a runner is: async (conn, target_key, hint) -> (records, resource_url, params)
MethodHint   = tuple[str | None, dict | None]     # the locator that won last time (None, None on first run)
MethodResult = tuple[list, str | None, dict | None]

async def run_target(
    conn, target_type, target_key, catalog, *,
    min_records=1,
    governor=None,                                 # ReExploreGovernor for staleness re-walks
    plausible=access.is_plausible,                 # your "is this a good record?" predicate
    max_yield=False, quality_key=len,              # "best data now" mode (walk full ladder, keep richest)
) -> tuple[str | None, list]:                      # (winning_method_name, records)
```

`run_target` tries the stored winner first, re-walks cheapest-first on failure/staleness, persists
each attempt in `access_methods`, and commits per attempt. Provide `plausible` when your records
aren't store-shaped (name + address/city/zip). `ReExploreGovernor` and `is_plausible`/`is_success` are
also in this module.

## `rung.queue` — the work queue (`jobs` table, `FOR UPDATE SKIP LOCKED`)

```python
queue.worker_id() -> str                                        # identifies this process
queue.enqueue(conn, task_type, target_key, payload=None) -> bool    # insert if no live job exists; caller commits
queue.claim_next(conn, task_type, worker, target_prefix=None) -> Job | None   # claim oldest pending; commits
queue.claim_target(conn, task_type, target_key, worker) -> Job | None         # claim one specific job
queue.complete(conn, job_id, status, *, worker, error=None) -> bool           # worker-scoped; caller commits
queue.reap_expired(conn, task_type) -> int                     # recover dead-worker leases
queue.prune_completed(conn, *, older_than_hours=168) -> int

@dataclass
class Job:
    id: int
    task_type: str
    target_key: str
    payload: dict | None
    attempts: int
```

## `rung.registry` — the plugin seam

```python
registry.register(name, impl, *, override=True) -> None    # provide a stage implementation
registry.resolve(name) -> Callable                         # the impl, or a stub raising StageNotAvailable
registry.load_plugins() -> list[str]                       # discover the rung.plugins entry-point group (idempotent)
registry.StageNotAvailable                                 # raised by a stub when an unplugged stage is invoked
```

Register stages under the names the CLI resolves (see `CONTRIBUTING.md`'s stage table), or any names
your own driver resolves.

## `rung.http` — the honest session chokepoint

```python
http.make_session(*, proxy=None, ...) -> AsyncSession      # the ONLY sanctioned way to get a client
http.set_impersonation(profile | None) -> None             # opt-in TLS impersonation (off by default)
http.HONEST_USER_AGENT                                     # the self-identifying default UA
```

All network access must go through `make_session()` (AST-enforced by `tests/test_http.py`).

## `rung.rate_limit` / `rung.browser`

```python
rate_limit.try_acquire(conn, host, *, rate_per_sec, burst, cost) -> bool   # cross-worker token bucket
browser.render_html(...) / browser.make_browser_options() / browser.get_script_value(...)  # pydoll/Chrome
```

---

See [`concepts.md`](concepts.md) for what these do together, and
[`build-your-own-domain.md`](build-your-own-domain.md) for a worked pipeline.
