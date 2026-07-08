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

## Publication related work (for the `rung` engine + the measurement paper)

Related work for the planned papers (see the private `publication_roadmap.md`) — the prior art a
reviewer expects us to position against, not code-grounding refs. Gathered 2026-07-08 (deep-research).

| Reference | Relevance | DOI / link | Local file |
|---|---|---|---|
| **Dalvi, Kumar & Soliman (2011). "Automatic Wrappers for Large Scale Web Extraction."** PVLDB 4(4). | JOSS Target-1 related work: wrapper induction at web scale; its running example is store-locator extraction (≈ our Stage 2). We differ by adding roster reconciliation + the persisted cost-ranked ladder. | [10.14778/1938545.1938547](https://doi.org/10.14778/1938545.1938547) · [arXiv 1103.2406](https://arxiv.org/abs/1103.2406) | `data/design_papers/dalvi_2011_wrapper-induction.pdf` |
| **Wang et al. (2026). "Co-Scraper: query-aware DOM Pruning and Reusable Scraper Synthesis."** | LLM-era "reuse not rebuild": synthesizes reusable wrapper CODE; we persist the winning access METHOD/tier per target — same motive, different mechanism. | [arXiv 2606.14821](https://arxiv.org/abs/2606.14821) | `data/design_papers/wang_2026_co-scraper.pdf` |
| **Smith et al. (2018). "Journal of Open Source Software (JOSS): design and first-year review."** | Target-1 venue calibration: defines what JOSS review targets (software, docs, tests, CI, license). | [arXiv 1707.02264](https://arxiv.org/abs/1707.02264) | `data/design_papers/smith_2018_joss.pdf` |
| **Gundelach, Mühlhäuser & Herrmann (2026). "Detecting Bot Detection: Prevalence, Techniques, and Implications for Web Measurement."** | **The IMC novelty anchor** — 83% of web-measurement papers ignore bot-blocking; provider-correlated sample loss. Our measurement angle must beat/differentiate from this. | [arXiv 2606.14525](https://arxiv.org/abs/2606.14525) | `data/design_papers/gundelach_2026_bot-detection.pdf` |
| **Azad, Starov, Laperdrix & Nikiforakis (2020). "Web Runner 2049: Evaluating Third-Party Anti-bot Services."** DIMVA. | Canonical measurement of the anti-bot defenses (fingerprinting > IP) rung encounters; grounds the TLS-impersonation discussion. | [10.1007/978-3-030-52683-2_7](https://doi.org/10.1007/978-3-030-52683-2_7) | `data/design_papers/azad_2020_anti-bot-services.pdf` |
| **Kim et al. (2025). "Scrapers Selectively Respect robots.txt Directives."** ACM IMC 2025. | The directly-adjacent IMC venue precedent (scraper robots.txt compliance); anchors the ethics/robots.txt framing. | [arXiv 2505.21733](https://arxiv.org/abs/2505.21733) | `data/design_papers/kim_2025_robots-txt.pdf` |
| **Kang et al. (2026). "Whose Agent Are You? Multi-Layer Fingerprinting and Attribution of Autonomous Web Agents."** | Current fingerprinting/attribution of automated agents — the defender's view of exactly what rung's honest-HTTP layer exposes. | [arXiv 2606.20910](https://arxiv.org/abs/2606.20910) | `data/design_papers/kang_2026_agent-fingerprinting.pdf` |

## Internal write-ups (context, not external refs)
- `docs/postgres_for_everything.md` — the "Postgres for everything" pattern (SKIP LOCKED queue, no broker).
- `docs/access_methods_design.md` — the cost-ranked, self-healing access-method registry + the RED-inspired governor.
- `docs/distributed_scraping_design.md` — the distributed layer (proxies, tiers, queue lease/heartbeat/reaper, token bucket, cron spread).

## Adding a reference
Drop the PDF in `intel/data/design_papers/` under `firstauthor_year_keyword.pdf`, add a row above,
and cite it inline in the relevant `docs/*_design.md`. This list is hand-maintained (no generator) —
it's a handful of engineering sources, not the ~80-paper science corpus.
