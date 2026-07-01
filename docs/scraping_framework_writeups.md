# Scraping-framework write-ups — outlines

Two planned write-ups about the **open-source scraping framework itself** (separate from the
cannabis-science papers): a short **systems/tools paper** and a **community narrative**. Both are
give-back-mission-aligned and both stay strictly at the **architecture/pattern** level — the *public*
half. **No per-platform recipes** (exact queries/endpoints/throttle params) — those stay private, per
the public-build redaction. Teach the design, not the exploit map.

Final artifacts land in the **public** repo (`paper.md`+`paper.bib` for JOSS; `NARRATIVE.md`); these
are the planning outlines. Engineering citations: `docs/design_references.md`.

---

## 1. Systems/tools paper — target: JOSS (Journal of Open Source Software)

JOSS papers are short (~250–1000 words): a Summary + Statement of need + a compact design/features
section + references. The public `richard-burhans/rung` core is the reviewed artifact.

**Working title:** *rung: a broker-free, Postgres-centric framework for resilient
distributed scraping of fragmented consumer-data platforms.*

**Authors:** Richard Burhans (+ PI / collaborators TBD).

### Summary (~120 words)
- What it is: an open-source Python framework for building authoritative, multi-state datasets from
  many fragmented consumer platforms — demonstrated on the US legal-cannabis market (licensed
  dispensaries → each operator's store list → each store's live menu: products, prices, potency,
  terpenes).
- The core idea: treat *every* target as reachable several ways at different cost, run the cheapest
  that works, remember it, and coordinate the whole fleet on Postgres alone (no broker).

### Statement of need (~150 words)
- The data problem: consumer-market data (menus, catalogs, listings) is scattered across many
  platforms with different access surfaces; collecting it reproducibly at scale is brittle and
  ad-hoc in practice. Cannabis is a sharp instance (fragmented, fast-churning, under-studied) but
  the pattern generalizes to any multi-platform consumer domain.
- The gap: most scrapers are single-platform, single-process, and fragile under blocking; scaling
  usually reaches for a message broker + a cache + a rate-limit service. We show a **broker-free**
  design that gets crash-safe distributed coordination, per-host politeness, and linear scaling from
  Postgres + an access-method abstraction.
- Audience: researchers building market datasets; the scraping community; anyone needing polite,
  resilient, reproducible multi-platform collection.

### Design / key features (the contributions — one short para each)
1. **Cost-ranked, self-healing access-method registry.** Each target has an ordered ladder of methods
   (static JSON → API → rendered browser → AI fallback); `run_target` runs the cheapest that works,
   persists the winner per target, and re-walks only on failure / a cheaper untried rung / a governed
   staleness check. A **RED-inspired re-exploration governor** (Floyd & Jacobson 1993) admits
   discretionary re-walks probabilistically to avoid a synchronized "thundering-herd" re-scrape.
2. **Postgres for everything.** A `FOR UPDATE SKIP LOCKED` work queue lets concurrent workers
   partition targets with no broker; **lease + heartbeat + reaper** give crash-safe recovery of dead
   workers; a per-host **token bucket** and durable **proxy health** live in the same database.
3. **Polite, linear scaling.** Per-IP rate limiting + **per-platform proxy tiering** (direct /
   datacenter / residential, chosen by an escalation gate) + one IP per worker → aggregate per-host
   throughput scales ~linearly with workers **without a distributed lock**.
4. **A publishable core behind a plugin seam.** A public/private split (entry-point plugin seam) lets
   the generic engine ship open-source while proprietary per-platform recipes stay in a private
   overlay — with a leak-guarded build.
5. **Reproducibility.** A one-command QA gate; optional export of the pipeline as a Galaxy workflow.

### Comparison (1–2 sentences)
- vs Scrapy/Nextflow-style crawlers: not a crawl framework but an *access-strategy* engine +
  broker-free distributed coordinator; the queue subsumes Scrapy's `JOBDIR` resumability across hosts.

### Availability & references
- MIT-licensed public core; link. References: Floyd & Jacobson 1993 (RED), Google "Reliable Cron
  across the Planet" (distributed-cron spread), AWS backoff/jitter, Postgres SKIP LOCKED docs.

### Open questions to resolve before writing
- Author list + which findings (if any) to cite as the "why it exists" motivation.
- How much of the multi-state coverage stats to include (JOSS is software-focused, not a results paper).
- Confirm JOSS scope fit vs a short arXiv/systems-workshop preprint.

---

## 2. Community narrative — a readable explainer (give-back)

Long-form, blog-style, in the JWR give-back tradition; lands as `NARRATIVE.md` in the public repo
(or a blog post). Goal: help a scraper-builder *understand the design and the reasoning*, with the
war stories that make the lessons stick. Architecture-level; recipes stay private.

**Working title:** *The cheapest thing that works: building a polite, resilient distributed scraper on
nothing but Postgres.*

### Arc
1. **The problem, honestly.** Fragmented consumer platforms; data that churns daily; why naive
   scraping "works on my machine" and then dies on a VPS.
2. **The philosophy.** Polite (per-IP pacing, back off first, rotate second), resilient (assume you'll
   be blocked and crash), reproducible (a green gate + published core), and give-back (the public core).
3. **Idea 1 — "the cheapest thing that works."** The access-method registry: try cheap, escalate only
   when needed, *remember the winner*, self-heal when it breaks. Why persisting the method-per-target
   is the whole game.
4. **Idea 2 — "Postgres for everything."** Why we didn't reach for Redis/Celery: `SELECT … FOR UPDATE
   SKIP LOCKED` is a work queue; leases + heartbeats + a reaper are crash recovery; token buckets and
   proxy health are just rows. One dependency, one backup, one mental model.
5. **Idea 3 — scaling politely.** Per-IP rate limiting; proxy tiers (when you actually need residential
   vs when datacenter is fine — with the escalation-gate test); one IP per worker so throughput scales
   linearly without a distributed lock.
6. **Idea 4 — not looking like abuse.** The RED-inspired governor: spreading discretionary re-walks so
   a same-day cohort doesn't re-scrape in one synchronized burst (a nod to Floyd & Jacobson).
7. **War stories (the teachable failures).**
   - The **406 volume soft-block**: a first ingest quietly under-captured because a host soft-blocks an
     egress over a rate threshold and the old fetch turned it into silent zero-yield — fixed with
     pacing + soft-block-aware retry; the paced re-ingest recovered a big chunk of the dataset.
   - The **"datacenter-IP block" myth**: several failures blamed on IP reputation were actually a
     *non-impersonating client* — TLS fingerprint, not IP. (Diagnose before you buy residential.)
   - The **zombie-schema test hang**: a killed test run left DB locks that wedged the next run's
     schema sweep — a lesson in making teardown robust (roll back before you drop; cap the lock wait).
8. **Takeaways for the community.** A short list of transferable rules.
9. **Use it.** What's open-source, how to run the public core, where the line is (recipes are yours to
   build — the engine is the gift).

### Tone / guardrail
- Warm, concrete, honest about failures. Every mechanism explained at the *pattern* level; **no exact
  per-platform queries/endpoints/throttle numbers** (those are redacted from the public build for a
  reason). The narrative should be publishable to the same audience as the public repo.

### Open questions
- Home: `NARRATIVE.md` in the public repo vs a hosted blog post (or both).
- Length/depth (a tight 2k-word essay vs a longer multi-part series).
