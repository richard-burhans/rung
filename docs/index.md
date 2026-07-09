# ![](assets/logo.svg){ .rung-mark } rung

**Run the cheapest rung that works.**

`rung` is a Postgres-centric web-scraping engine built around one idea: any target you want to
extract is reachable several ways at different cost and reliability — a cheap static-JSON endpoint,
a rendered page, an LLM extraction — so the engine runs the **cheapest method that works**, remembers
the winner per target, and only re-walks the ladder when that winner fails or goes stale.

It leans on Postgres for everything a scraping fleet needs — a `FOR UPDATE SKIP LOCKED` work queue
with leases/heartbeats (no Redis or Celery), the per-target access-method registry, and cross-worker
rate limiting — behind an honest-by-default HTTP layer (opt-in TLS impersonation) and a plugin seam
for per-domain catalogs.

!!! tip "Start with the story"
    **[The cheapest thing that works](https://github.com/richard-burhans/rung/blob/main/NARRATIVE.md)**
    is the narrative behind `rung` — the design, the reasoning, and the failures that taught us the
    design. It's the most enjoyable way in, and the best way to understand why the engine is shaped
    the way it is. The pages below are the reference; that's the story.

## Start here

<div class="grid cards" markdown>

- :material-rocket-launch: **[Quickstart](quickstart.md)** — clone → Postgres → run one command → see
  the ladder resolve.
- :material-lightbulb-on: **[Concepts](concepts.md)** — the four load-bearing ideas: the cost-ranked
  access ladder, the SKIP-LOCKED queue, the plugin seam, the honest-HTTP chokepoint.
- :material-hammer-wrench: **[Build your own domain](build-your-own-domain.md)** — drive the engine as a
  library for a non-cannabis pipeline, end to end.
- :material-api: **[Engine API](api.md)** — the surface a pipeline or plugin author calls.

</div>

## The engine vs. the reference application

The engine is domain-agnostic. The cannabis-dispensary pipeline that ships with it is the
**reference application** — one worked way to use the engine (reconcile government license rosters
against operators' own store lists and live menus), not the only way. Its schema and stages live
behind `db.create_reference_tables()` / the `reference_db` module, cleanly separated from the generic
engine so `import rung.db` loads none of it. The [reference-application docs](stage_contracts.md) are
labelled as such.

## Design & background

- [Postgres for everything](postgres_for_everything.md) — why the queue, registry, and rate limiter
  all live in Postgres.
- [Public/private split](publish_split_design.md) — how the open-source core and the private overlay
  divide, and how the plugin seam holds the boundary.
- [`ARCHITECTURE.md`](https://github.com/richard-burhans/rung/blob/main/ARCHITECTURE.md) —
  the module map and dependency edges.
- [`NARRATIVE.md`](https://github.com/richard-burhans/rung/blob/main/NARRATIVE.md) — the
  "why it's built this way" story.
