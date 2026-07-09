# Store history — tracking dispensary lifecycle (open / close / acquired)

> **Reference application (dispensary dataset).** This document describes the reference pipeline that ships with `rung`, not the generic engine. If you are building your own domain, see [`build-your-own-domain.md`](build-your-own-domain.md) — you define your own equivalents.

Status: **Phase 0 (company-site capture) + Phase 0.5 (roster capture) — 2026-07-02;
Phase 1 (lifecycle derivation, `store-lifecycle`) + Phase 2 (materialized
`store_lifecycle_events`, `--write`) — 2026-07-09. Relocation collapsing deferred: no real case
exists to tune against yet.**

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

This is the store-level analogue of the master-product-DB pattern already shipped (`products` +
append-only `product_observations`), and roster-churn / closure detection is one of the
high-readiness future analyses tracked in our replication backlog. The *analysis* can wait for
history to accrue; the **capture cannot** — every un-logged day is unrecoverable. So Phase 0
(capture) ships now; the derivation is later.

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
  (already threads it to menus) now also drive Stage-2 capture. **Ops (live):** a weekly store-history
  sweep runs `scrape-company-stores --record-history` on the host, Sundays 12:00 (see the deployment
  runbook); first store-history accrual is 2026-07-05.

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
  the same weekly store-history sweep also runs `scrape-states --record-history`.

Both legs go through one identity helper, `dedupe.location_key` = `geo_key` with a **guarded**
`address_key` fallback. The guard is load-bearing for the roster: some states put a non-address in
the address field — MD stores a bare COUNTY (live-verified 2026-07-02: 119 MD roster rows collapsed
to 23 county-level keys under an ungated fallback, which would fabricate operator-change events). So
the address fallback fires only when the row has a house-number digit AND a 5-digit zip; weaker rows
are unidentifiable and skipped (don't guess). MD's county-only roster therefore records nothing on
the roster leg until those rows gain street addresses/geocodes — its stores are still captured on the
`company_site` leg (99 % geocoded).

The two independent sources enable the cross-source corroboration below. **Known limitation (resolved
in Phase 1):** a store geocoded by one source but address-only in the other gets two `location_key`s
(geo vs address form). The Phase-1 derivation folds them at read time — see "Identity fold" below —
mirroring how `compare-stores` matches rosters to own-site rows. Capture stays raw.

## Phase 1 — derive lifecycle (read-side; SHIPPED 2026-07-09)

`store-lifecycle --state XX [--closed-after-cycles K]` → `rung_intel/store_lifecycle.py`, registered
through the plugin seam like `compare-stores`. Reads only; Phase 2 (below) materializes the events.

- **opened** = a location's first sighting, *excluding* the source's earliest usable cycle (that is
  the initial backfill, when every store "appears") and excluding sightings that fall in a partial
  cycle (too thin to assert an opening, just as it is too thin to assert an absence). Both are
  reported as `first seen`. Judgement is tracked **per source**: a roster leg with plenty of history
  must not make an unjudgeable own-site leg read as "nothing closed".
- **closed** = absent for **K consecutive usable scrape cycles of its state and source** (default
  K=2). Cadence-aware, never wall-clock: the sweep is weekly and covers a different set of states
  each run (2026-07-04 touched 3 states, 2026-07-05 touched 41), so a global day-grid would declare
  every unscraped state closed.
- **acquired / rebranded** = the canonicalized operator at a stable location changes.
- **Cross-source corroboration**: gone from own-site but still on the roster = "closed, roster
  lagging" — the project's core thesis, now dated. (First live hit 2026-07-09: one Ontario store, at
  `K=1`, last on its operator's own site 07-04 and still on the province's roster 07-05. Named
  examples stay out of the design doc: an unreviewed `K=1` verdict about a real business is a claim
  we have not earned the right to publish.)

Three things the live data forced, each of which a naive version gets wrong:

1. **Identity fold** (the Phase-0.5 limitation above). 1,283 address-keyed locations are the same
   physical store as a geo-keyed one; unfolded, each is a phantom "opened" and cross-source
   corroboration can never match. We fold an address-keyed location into a geo-keyed one when they
   share a `dedupe.address_key` **and exactly one** geo location claims it (1,210 of the 1,283). The
   other 73 are ambiguous — two or three geo cells share the key, and sampled pairs sit >100 m apart,
   i.e. genuinely different stores. Ambiguous means skip, don't guess.

2. **Operator identity tolerates storefront drift.** `canon` folds casing, generics and
   companies.yml aliases but not the locality suffix a site tacks on. The discriminator is the one
   this doc's own survey draws: drift **extends** the operator name, an acquisition **replaces** it.
   So two canonical names denote the same operator when their spelling-folded brands are equal or one
   prefixes the other. "ZEN LEAF"/"ZEN LEAF MERIDEN" and "Kindling Cannabis"/"Kindling Cannabis North
   York East" are drift; "Columbia Care"/"gLeaf" and "Pop's Cannabis Co."/"Fika Local" are real.
   Without this, ON reported 4 acquisitions in one day; with it, 1 — the real one. The prefix branch
   knowingly merges two distinct operators when one brand prefixes the other ("Bloom"/"Bloomfield"):
   that under-reports, and inventing an acquisition is far worse than missing one.

3. **Partial cycles are not evidence of absence.** The own-site leg scrapes per company, so a failed
   company writes nothing and its stores look absent. A partial run is a dip the source **later
   beats**; a genuine decline is never beaten later, so the comparison looks *forward only* — never
   at the widest cycle overall, which would suppress the closures of a state that really shrank. ON's
   own-site sweep saw 1,106 locations on 07-04 against 1,280 on 07-05 (86%), so 07-04's absences are
   discarded. Presence in a partial cycle still counts: a store seen there was really there.

   Guarding on *operator* presence instead is tempting and **wrong**: a single-store operator vanishes
   from a cycle exactly when its one store closes, which would make those closures — the common case —
   undetectable. Operator presence instead grades **confidence**: `corroborated` when the operator's
   other stores were still being scraped while this one went missing, `unconfirmed` when the operator
   vanished wholesale (scrape failure and total exit are indistinguishable from the log alone).

## Phase 2 — events table + surfacing (SHIPPED 2026-07-09; relocation tuning deferred)

`store-lifecycle --state XX --write` materializes the derivation into `store_lifecycle_events` so
maps and the patient UI read events directly instead of re-deriving:

```
store_lifecycle_events                -- the DERIVED conclusion; recomputed, never appended
  id                  BIGINT PK
  location_id         BIGINT NOT NULL -- → store_locations (display fields live there; join)
  state, source       TEXT
  kind                TEXT            -- opened | closed | operator_changed | relocation_candidate
  occurred_on         DATE
  operator, previous_operator  TEXT
  confidence          TEXT            -- closed: corroborated | unconfirmed
  related_location_id BIGINT          -- relocation_candidate: the location it may have moved FROM
  derived_at          TIMESTAMPTZ
```

**Replaced per state, not appended, and deliberately allowed to shrink.** `store_observations` is the
evidence and must never be lost; this table is a *conclusion* drawn over all of it. Later evidence
revises an earlier verdict — a store called "closed" that reappears next cycle was never closed — so
`db.replace_lifecycle_events` does a wholesale per-state replace. The keep-the-best guard that
protects `replace_company_stores` would be actively wrong here: that guard stops a transient scrape
failure from clobbering a live snapshot, whereas *fewer events* is a legitimate re-derivation.

### Relocation: reported, never collapsed — and NOT tuned

The remaining edge case is "a store relocating a few blocks vs a genuinely new one". Today it
**cannot be tuned**: the observation log holds **two dates**, and no state has yet produced an opening
and a closure together, so there is not one real relocation to calibrate a radius against. Inventing a
"within N metres" threshold and calling it tuned would be the guess this project refuses everywhere
else.

So `relocation_candidates` carries **no distance threshold**. A candidate is a closure and an opening
on the same source, with the same canonical operator, in the same town (`compare.norm_city` — the fold
`compare-stores` already uses, not a new one), where the opening does not precede the closure. Both the
`opened` and `closed` events still stand; a `relocation_candidate` row merely links them via
`related_location_id`. A consumer may merge them; the derivation does not decide.

**Revisit when history matures:** once several states have produced opening/closure pairs, calibrate
against the real cases and decide whether to collapse.

## Reuse (don't rebuild)

- `rung/sources/dedupe.py` — `geo_key`, `address_key`, `normalize_address` (the identity key).
- `rung/db.py` — `record_observations` / `products` (the append-only pattern + change-vs-daily
  discipline to mirror).
- `text.extract_brand` + `companies.yml` — operator canonicalization for the acquisition signal
  (Phase 1), the same `canon()` used by `compare-stores`.
