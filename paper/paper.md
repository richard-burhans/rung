---
title: '`rung`: a broker-free, Postgres-centric framework for resilient distributed scraping of fragmented consumer-data platforms'
tags:
  - Python
  - web scraping
  - data engineering
  - distributed systems
  - PostgreSQL
  - reproducibility
authors:
  - name: Richard Burhans
    orcid: 0009-0006-5949-0958
    affiliation: 1
  # TODO: add PI + collaborators (with ORCIDs) once confirmed
affiliations:
  - name: The Galaxy Project, Penn State University, USA
    index: 1
date: 1 July 2026
bibliography: paper.bib
---

# Summary

`rung` is an open-source Python framework for assembling authoritative, multi-state
datasets from many fragmented consumer-data platforms. It was built to map the U.S. legal-cannabis
market — the licensed dispensaries in each state, each operator's own published store list, and each
store's live menu (products, prices, potency, and terpene content) — but its design is
domain-agnostic. Its organizing idea is that any given target is reachable in several ways at very
different cost, so the framework runs the *cheapest method that works*, remembers which one did per
target, and coordinates a fleet of workers on PostgreSQL alone — with no message broker, cache, or
separate rate-limit service. The result is a scraper that becomes cheaper as it runs, recovers
cleanly from both crashes and blocking, scales politely across many egress addresses, and ships with
a one-command reproducibility gate. A public core is published under a permissive license; the
platform-specific extraction recipes load through a plugin seam and are kept in a private overlay.

# Statement of need

Assembling reproducible datasets from consumer-market platforms — menus, catalogs, storefront
listings — is a recurring need across computational social science, market research, and
public-health surveillance. The U.S. cannabis market is a sharp example: the data are scattered
across many consumer platforms, each with a different access surface; listings churn daily; and the
market is large but understudied at scale. The collection problem, however, generalizes well beyond
cannabis.

In practice, scrapers tend to be single-platform, single-process, and brittle the moment a target
begins to block — and scaling one usually means adding a message broker, a cache, and a rate-limit
service. `rung` offers a smaller design that still gives crash-safe distributed
coordination, per-host politeness, and near-linear scaling: an *access-method abstraction* plus
"PostgreSQL for everything." It is intended for researchers and engineers who need polite, resilient,
reproducible multi-platform collection, and it demonstrates a reusable pattern — a cost-ranked,
self-healing access registry coordinated entirely through a relational database — that transfers to
any domain where data is spread across many defensive, fast-changing sources.

# Design

The framework is organized around a few load-bearing ideas.

**A cost-ranked, self-healing access-method registry.** Each target has an ordered ladder of methods
(a static JSON endpoint, a documented API, a rendered browser, an AI-assisted extraction). A single
entry point runs the cheapest method that succeeds, persists the winner per target, and re-walks the
ladder only on failure, when a cheaper untried rung appears, or on a governed staleness check. To
avoid a synchronized "thundering-herd" re-scrape when a same-day cohort goes stale together, a
governor admits discretionary re-checks probabilistically, borrowing the idea of Random Early
Detection [@floyd1993].

**PostgreSQL for everything.** A `SELECT … FOR UPDATE SKIP LOCKED` work queue lets concurrent workers
partition targets with no coordinator [@postgres_skiplocked]; leases, heartbeats, and a background
reaper give crash-safe recovery of dead workers; and per-host token buckets and durable proxy health
live in the same database. Scheduled work is spread deterministically across a window to smooth load,
following the distributed-cron pattern [@davidovic2015], with jittered retries [@brooker2015].

**Polite, near-linear scaling.** Because the throttles that matter are per-address, assigning each
worker its own egress IP with a per-IP rate limiter lets aggregate per-host throughput grow roughly
linearly with the number of workers, without any distributed lock. Proxy tiers (direct, datacenter,
residential) are chosen per platform by a block-rate test, so the expensive tier is used only where
it is actually required.

**A publishable core behind a plugin seam.** A public/private split lets the generic engine ship as
open-source software while proprietary per-platform recipes remain in a separate overlay, guarded by
a leak-checked build.

**Reproducibility.** A single-command quality gate (lint, type-check, tests) accompanies the code,
and the pipeline can be exported as a Galaxy workflow for end-to-end reproduction.

Unlike general crawl frameworks such as Scrapy, `rung` is not a crawler but an
*access-strategy engine* and broker-free distributed coordinator; its work queue subsumes the
resumability such frameworks provide, and extends it across concurrent and multi-host workers.

# Availability

`rung` is released under the MIT License at
<https://github.com/richard-burhans/rung>. A companion narrative (`NARRATIVE.md`)
describes the design and the lessons behind it for the wider scraping community.

# Acknowledgements

We thank the open-source scraping and Galaxy communities whose tools and generosity made this work
possible.

# References
