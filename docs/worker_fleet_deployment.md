# Worker-fleet deployment (distributed Stage-2/3 scraping)

The next Tier-2 step after the queue hardening (#16), the per-host token bucket (#17), and
per-platform proxy tiering: run **N worker processes** that partition the work via the Postgres
`jobs` queue, each pinned to its **own egress IP**, so aggregate per-host throughput scales
~linearly with workers/IPs **without a distributed lock**. See `docs/distributed_scraping_design.md`
for the rationale (the throttle is per-IP → one IP per worker makes the in-process `the rate limiter`
the correct unit of control).

## What's already in place (merged)
- **`jobs` queue** — claim via `FOR UPDATE SKIP LOCKED`; `enqueue`/`claim_next`(scoped)/`complete`.
- **Lease + heartbeat + reaper** (#16, now WIRED) — `jobs.lease_until`/`last_heartbeat`;
  `reap_expired()` requeues lease-expired jobs (SKIP LOCKED) so a crashed worker's targets recover.
  Each runner (`run_store_menus`/`run_company_stores`) now auto-runs a **per-worker heartbeat**
  (`queue.heartbeat_forever` on a dedicated connection, extending all of this process's in-flight
  leases) and reaps expired leases at startup; the `reap-jobs` CLI is the standalone reaper cron.
- **Per-host token bucket** (#17) — `token_buckets` + `rate_limit.try_acquire()` for shared-IP safety.
- **Proxy health + tiering** — `proxies` table (per proxy×host health) + `proxy_store`; per-platform
  tier selection (`proxy_tiers`: aggregators→residential, Dutchie/others→datacenter/direct).

## Deployment model
- **Host:** the DigitalOcean VPS already used for the daily sweep (Tailscale → host Postgres on the
  tailnet; daily pg_backups). One VPS can run several workers; scale out to more VPSes for more IPs.
- **One IP per worker.** Assign each worker a distinct proxy URL (or a provider "sticky session"
  username) so per-(worker×host) pinning keeps TLS+cookies+IP coherent. Residential pool for the
  aggregator workers, datacenter (or host-direct) for Dutchie/Jane/etc. per the tier policy.
- **Shared state = Postgres only.** No broker, no Redis — the queue + proxies + token_buckets tables
  are the coordination substrate ("Postgres for everything").

## Per-worker loop (what each process does)
1. `requeue_stale`/`reap_expired` opportunistically at startup (recover crashed-run claims).
2. `claim_next(conn, task_type, worker_id, target_prefix=…)` — SKIP LOCKED, so workers partition.
3. Run the Stage-2 (`scrape-company-stores`) or Stage-3 (`scrape-menus`) catalog for the target,
   acquiring a proxy from the platform's tier pool (`proxy_tiers.pool_for_platform`). Stage 2 then
   claims the healthiest exit for the company's network host via `proxy_store.claim_proxy` (durable
   cross-worker health); Stage 3 instead pins one exit **per store** in-process
   (`ProxyPool.acquire(host=store_key)`) — per-store rotation, not per-host claiming. This is
   deliberate given one-IP-per-worker (see ARCHITECTURE.md "Known asymmetries" / audit M1).
4. The per-worker heartbeat is **automatic**: the runner launches `queue.heartbeat_forever(worker_id)`
   on its own connection for the life of the batch, extending all of this process's in-flight leases
   (no per-job `bump_heartbeat` call needed in the loop).
5. `complete(conn, job_id, status, worker=worker_id)` (only if still the holder) — commit atomically
   with the data writes. On failure, the proxy is benched — Stage 2 via `proxy_store.report_proxy(ok=False)`
   (durable), Stage 3 via the in-process `ProxyPool.report(ok=False)` (per the asymmetry in step 3).

## Bring-up steps
1. **Provision proxies** — populate the per-tier pools: `DISPENSARY_PROXIES_DATACENTER` /
   `DISPENSARY_PROXIES_RESIDENTIAL` (env or the gitignored per-tier files). Keep Dutchie host-direct
   where the escalation gate says datacenter/direct suffices (~76% of volume → ~$0 proxy bandwidth).
2. **Escalation gate** — run `scripts/escalation_gate.py` per platform on the VPS egress; it records
   the tier into `proxy_tiers` (datacenter suffices <10% blocked; escalate >30%).
3. **Enqueue** — a scheduler run seeds `jobs` for the day (hash each target's `run_at` across the
   window for an even, deterministic spread — Google-SRE distributed-cron; jitter each retry).
4. **Launch N workers** — one process per IP (systemd unit or a supervisor), each with a unique
   `worker_id`, the right tier env, and `DATABASE_URL` → the tailnet Postgres. Start small (2–4),
   watch block rates, scale up.
5. **Reaper** — run the `reap-jobs` CLI on a short cron (a dedicated tiny process or one worker's
   duty) so lease-expired jobs from crashed workers requeue promptly. It reaps `store_menu`,
   `company_stores`, and `dedupe` in one pass. The runners also reap at startup, so this cron is the
   belt-and-suspenders path for a fleet where no runner has restarted recently.
6. **Prune** — `prune_completed` on a daily cron so the `jobs` table's done/failed history doesn't
   slow the SKIP-LOCKED claim scans.

## Sizing & tuning
- **Throughput** ≈ workers × per-IP rate. The in-process `the rate limiter` (concurrency + spacing)
  is per worker; raise workers/IPs, not per-IP rate, to scale a host.
- **Bandwidth** — ~0.85 KB/product → ~4 GB/full national sweep. Residential bills per-GB, so keep
  Dutchie host-direct and put only the aggregators (Weedmaps/Leafly) on residential (~1 GB
  residential ≈ ~1.2 M products, per `docs/proxy_scrape_speed.md`).
- **Per-host circuit breaker** — if a host blocks everyone, stop bleeding IPs at it; back off the host.

## Observe
- **Queue depth / age** — `jobs` by status; a growing `pending` backlog ⇒ add workers.
- **Block rates** — per-host non-200 rate (the escalation-gate signal); a rising 406/403 ⇒ re-tier.
- **Proxy health** — `proxies` (consecutive_fails / disabled_until per proxy×host).
- **Token buckets / limiter** — `token_buckets` tokens per host under shared-IP fallback.
- Grafana views are sketched in `docs/distributed_scraping_design.md` §8; thresholds tune live.

## Worker entrypoint (`worker` CLI)
The per-worker loop above is now a first-class command — `uv run worker --state PA,NJ` — instead of
the ad-hoc scoped-claim path. It reaps crashed leases and drains the queue for the given states
(`--task menus|company-stores|both`), carrying the same freshness/aggregator/history flags as
`scrape-menus`; `--poll-seconds N` keeps a long-lived process re-draining (0, the default, drains
once and exits — the cron shape). One process per egress IP; the tier env (`DISPENSARY_PROXIES_*`)
and a unique `worker_id` are the only per-process differences. The runners still own the heartbeat
and startup reap, so a bare `scrape-menus`/`scrape-company-stores` remains an equivalent single-stage
worker; `worker` just packages the reaper + both stages + the poll loop.

## Open items (not blocking bring-up)
- Decide worker orchestration beyond a single VPS (VM pool vs k8s CronJob→Job vs Cloud Run) —
  ~7k jobs/day fits any; revisit when a single box saturates its IP budget.
