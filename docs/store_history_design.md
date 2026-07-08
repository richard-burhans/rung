# Store history — tracking dispensary lifecycle (open / close / acquired)

> **Reference application (dispensary dataset).** This document describes the reference pipeline that ships with `rung`, not the generic engine. If you are building your own domain, see [`build-your-own-domain.md`](build-your-own-domain.md) — you define your own equivalents.

Status: **design + Phase 0 (company-site capture) + Phase 0.5 (roster capture) — 2026-07-02.**

## Why

`company_stores` (the operator's own store list) and `dispensaries` (the state roster) are both
**destructive live snapshots** — `db.replace_company_stores` does `DELETE + re-INSERT` every scrape,
`scrape-states` overwrites the roster. When a store closes, its row is deleted and **the transition is
lost forever**; when one opens, it appears with no "first seen" record. Today we hold ~16.5k current
company-store rows and **zero history**.

That throws away exactly the signal this project exists to surface, in its most valuable (temporal)
form. The core deliverable is already "operator's own site vs the state roster — where does the state
list lag?" Store lifecycle is that comparison *over time*: "the state still lists a dispensary that
closed in March" becomes a dated, provable fact. Openings / closures / acquisitions per state are
also a genuine market-structure signal (useful for the analysis/CSHL angle and for patients: "is my
dispensary still open?").

This is the store-level analogue of the master-product-DB pattern already shipped
(`products` + append-only `product_observations`, see `master_product_db_design.md`) and is listed as
a high-readiness future analysis in `reports/replication_and_future_work.md` §8 ("roster-churn /
closure detection"). The *analysis* can wait for history to accrue; the **capture cannot** — every
un-logged day is unrecoverable. So Phase 0 (capture) ships now; the derivation is later.

## The hard part: a stable identity (verified against live data)

A store's row mutates day to day — address reformatting (`130 Mall Cir Dr` vs `130 Mall Circle
Drive`), operator renames, roster-only → menu-handled. So the design hinges on an identity that
survives those changes. Measured on the live data (2026-07-02):

- **Coordinates alone over-merge.** At ~11 m precision, 16,503 coord-bearing company-store rows
  collapse to 14,134 buckets while there are 15,868 distinct named stores, and **1,888 rounded
  buckets hold >1 operator** — a naive GPS grid would fabricate ~1,888 false "acquisition" events.
- **Those collisions are alias/casing drift, not co-location.** Sampling them: `A PRIME LEAF` /
  `A Prime Leaf`, `ZEN LEAF` / `ZEN LEAF MERIDEN`, `Nirvana Center` / `Nirvana Center Phoenix` — plus
  one genuine rebrand (`Columbia Care` / `gLeaf`, which is *exactly* the acquisition signal we want).

Conclusion — reuse the identity primitives the dedupe layer already has (measured "zero false merges"
at these settings), don't invent a raw grid:

- **Location identity** `location_key = dedupe.geo_key(lat, lng, zip) or dedupe.address_key(address,
  zip)` — an ~11 m coord cell + 5-digit zip (`rung/sources/dedupe.py`, `_GEO_PRECISION = 4`), with the
  normalized-address+zip key as the documented fallback for the ~36 %-geocoded roster rows. This is
  the *physical location*, stable across text/operator churn.
- **Operator is a time-varying *attribute* of a location, not part of its identity.** A real
  "operator changed" event = the **canonicalized** operator (via `text.extract_brand` + the
  `companies.yml` alias fold — the same `canon()` `compare-stores` uses) at a stable location changes.
  Raw `canonical_name` carries the casing/suffix drift above, so it is stored raw for fidelity but
  **canonicalized at read time** (mirrors `product_observations` keeping raw values).

## Schema (Phase 0 — shipped)

Two tables, mirroring `products` / `product_observations`:

```
store_locations                       -- one row per distinct physical location ever seen
  id            BIGINT PK
  location_key  TEXT UNIQUE           -- geo_key or address_key (the stable identity)
  state         TEXT
  latitude, longitude  DOUBLE PRECISION
  address, city, zip_code  TEXT       -- representative raw components (display + key recompute)
  first_seen, last_seen  TIMESTAMPTZ

store_observations                    -- APPEND-ONLY log; never updated or deleted
  id            BIGINT PK
  location_id   BIGINT NOT NULL       -- FK → store_locations
  source        TEXT                  -- 'company_site' (Phase 0) | 'state_roster' (Phase 0.5)
  operator      TEXT                  -- raw canonical_name (canonicalized at read)
  storefront_name  TEXT               -- display alias
  platform, external_id  TEXT         -- the menu handle at the time (handle churn is also signal)
  address, city  TEXT                 -- as-observed (may drift)
  observed_at   TIMESTAMPTZ
```

Write discipline (identical to `record_observations` for products): upsert the `store_locations`
identity (keep `first_seen`, bump `last_seen`, refresh representative attrs), then **append** a
`store_observations` row only when the observed attributes changed since the last observation for that
`(location_id, source)`, **or once per day as a presence heartbeat**. Append-only; partition later if
it grows.

## Write path (Phase 0 — shipped)

`company_stores.record_store_observations(conn, company_id, state)` (overlay — it needs the `dedupe`
key builders, which sit above `db` in the layering) reads the company's **current stored**
`company_stores` rows (so it heartbeats correctly even when the keep-the-best replace *rejected* a
low-yield re-scrape — the store is still alive) and applies the write discipline above via the
shared engine (`db.record_location_observations`, see Phase 0.5).

Wired behind the **existing `record_history` flag**, exactly like product history:
- `run_company_stores(..., record_history=False)` calls `record_store_observations` per company, in
  the same transaction as the keep-the-best replace + job completion.
- `scrape-company-stores --record-history` (new flag) and the self-feeding `worker --record-history`
  (already threads it to menus) now also drive Stage-2 capture. **Ops (live):** the weekly
  `store_history_sweep.sh` cron runs `scrape-company-stores --record-history` — installed on the host,
  Sundays 12:00 (see `deployment_runbook.md`); first store-history accrual is 2026-07-05.

Capture is intentionally from the **raw** per-scrape snapshot (not the alias-folded dedupe output):
keep the raw signal, canonicalize on read — so a future improvement to brand-folding re-derives
cleanly instead of being baked in lossily.

## Phase 0.5 — roster capture (shipped)

`source='state_roster'` capture from the state rosters, behind `scrape-states --record-history`.
Both captures now drive one **shared engine**, `db.record_location_observations(conn, source,
observations)` — the caller builds one `LocationObservation` (models.py) per `location_key` with its
own collapse rule; the engine owns the upsert + change-vs-heartbeat discipline:

- **Stage 2** — `company_stores.record_store_observations` (overlay): reads the company's stored
  rows, collapses same-rooftop aliases preferring the handle-bearing row, `source='company_site'`.
- **Stage 1** — `extract.record_roster_observations` (public core): builds observations from the
  just-extracted roster records inside `run_extract_states`' non-empty-replace commit,
  `source='state_roster'`. Runs ONLY on a non-empty extraction — a failed roster fetch records
  nothing, so an observed absence stays a real signal rather than an artifact of a dead list URL.
  The roster is only ~36 % geocoded, so its identity leans on the address fallback. **Ops (live):**
  the same weekly `store_history_sweep.sh` cron also runs `scrape-states --record-history`.

Both legs go through one identity helper, `dedupe.location_key` = `geo_key` with a **guarded**
`address_key` fallback. The guard is load-bearing for the roster: some states put a non-address in
the address field — MD stores a bare COUNTY (live-verified 2026-07-02: 119 MD roster rows collapsed
to 23 county-level keys under an ungated fallback, which would fabricate operator-change events). So
the address fallback fires only when the row has a house-number digit AND a 5-digit zip; weaker rows
are unidentifiable and skipped (don't guess). MD's county-only roster therefore records nothing on
the roster leg until those rows gain street addresses/geocodes — its stores are still captured on the
`company_site` leg (99 % geocoded).

The two independent sources enable the cross-source corroboration below. **Known limitation:** a
store geocoded by one source but address-only in the other gets two `location_key`s (geo vs address
form) — the Phase-1 derivation should fold same-`normalize_address` locations when corroborating,
mirroring how `compare-stores` matches rosters to own-site rows.

## Phase 1 — derive lifecycle (read-side, later; gated on accrued history)

A `store-lifecycle` report scanning `store_observations` grouped by `location_id`, ordered by
`observed_at`:
- **opened** = first observation of a location;
- **closed** = a location absent for **K consecutive scrape cycles of its state** (cadence-aware, NOT
  wall-clock days — a skipped cron run must not fake a closure);
- **acquired / rebranded** = the canonicalized operator at a location changes.
- **Cross-source corroboration** (needs Phase 0.5): gone from **both** own-site and roster =
  high-confidence closed; gone from own-site but still on the roster = "closed, roster lagging" — the
  project's core thesis, now dated.

## Phase 2 — events table + surfacing (later)

Materialize `store_lifecycle_events` (opened/closed/operator_changed) for maps + the patient UI to
read directly; tune the fuzzy-merge edge cases (a store relocating a few blocks vs a genuinely new
one) against real observed cases.

## Reuse (don't rebuild)

- `rung/sources/dedupe.py` — `geo_key`, `address_key`, `normalize_address` (the identity key).
- `rung/db.py` — `record_observations` / `products` (the append-only pattern + change-vs-daily
  discipline to mirror).
- `text.extract_brand` + `companies.yml` — operator canonicalization for the acquisition signal
  (Phase 1), the same `canon()` used by `compare-stores`.
