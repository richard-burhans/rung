# Design references

Engineering/CS sources that ground the architecture's named abstractions — kept **separate from
the cannabis-science literature** (`reports/bibliography.md`, whose "Missing — to download" list is
for domain papers). PDFs, when we hold them, live off-repo under `intel/data/design_papers/`
(like the science library) and are referenced here as code paths.

## References

| Reference | Grounds (code) | Status | DOI / link | Local file |
|---|---|---|---|---|
| **Floyd, S. & Jacobson, V. (1993). "Random Early Detection Gateways for Congestion Avoidance."** IEEE/ACM Trans. Networking 1(4), 397–413. | `access.ReExploreGovernor` — the "gentle-RED linear ramp rather than classic RED's hard knee" staleness admission control (`docs/access_methods_design.md` §"RED-inspired staleness governor") | ✅ have | [10.1109/90.251892](https://doi.org/10.1109/90.251892) · [PDF](https://www.icir.org/floyd/papers/early.pdf) | `data/design_papers/floyd-jacobson_1993_red.pdf` |
| **"Reliable Cron across the Planet"** (Google), ACM Queue 13(3), 2015 (also SRE Book ch. 24). | the deterministic **`run_at` spread** that hashes each target across the daily window to avoid a thundering herd (`queue.enqueue(spread_seconds=…)`, `docs/distributed_scraping_design.md` §4-5) | ⬇ to download (free) | [queue.acm.org](https://queue.acm.org/detail.cfm?id=2745840) | — |
| **Brooker, M. "Exponential Backoff and Jitter."** AWS Architecture Blog, 2015. | full-jitter retry backoff on requeue + the aggregator 406/429 governor (`aggregator_http`, requeue jitter) | ⬇ to fetch (free) | [aws.amazon.com/…/exponential-backoff-and-jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) | — |
| *(optional)* **Auer, P., Cesa-Bianchi, N. & Fischer, P. (2002). "Finite-time Analysis of the Multiarmed Bandit Problem."** Machine Learning 47, 235–256. | *only if* we make the cost-ranked access-method registry a principled **explore/exploit** selector (UCB1) — currently deferred by design (`docs/access_methods_design.md` §"deferred until we have evidence we need it") | ⏸ deferred | [10.1023/A:1013689704352](https://doi.org/10.1023/A:1013689704352) | — |

## Internal write-ups (context, not external refs)
- `docs/postgres_for_everything.md` — the "Postgres for everything" pattern (SKIP LOCKED queue, no broker).
- `docs/access_methods_design.md` — the cost-ranked, self-healing access-method registry + the RED-inspired governor.
- `docs/distributed_scraping_design.md` — the distributed layer (proxies, tiers, queue lease/heartbeat/reaper, token bucket, cron spread).

## Adding a reference
Drop the PDF in `intel/data/design_papers/` under `firstauthor_year_keyword.pdf`, add a row above,
and cite it inline in the relevant `docs/*_design.md`. This list is hand-maintained (no generator) —
it's a handful of engineering sources, not the ~80-paper science corpus.
