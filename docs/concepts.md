# Concepts

The four load-bearing ideas behind `rung`. Each is a small, orthogonal mechanism; a domain pipeline
(like the cannabis reference application, or the farmers-market example) is just these four wired
together. For the longer-form story see [`../NARRATIVE.md`](https://github.com/richard-burhans/rung/blob/main/NARRATIVE.md) and
[`postgres_for_everything.md`](postgres_for_everything.md).

## 1. The cost-ranked access ladder (`access.py`)

A target can usually be reached several ways at wildly different cost — a cheap static-JSON endpoint,
a hidden API, a rendered page, an LLM extraction. Instead of committing to one, you give the engine a
**catalog** of `AccessMethod`s ranked by try-priority, and `run_target` **runs the cheapest that
works, persists the winner per target, and re-walks the ladder only when that winner fails** (or a
cheaper untried rung appears, or a governed staleness re-explore fires). So the common case is one
cheap call, and resilience is automatic — a site that changes shape self-heals to the next method that
works. This is the idea the project is named for: *run the cheapest rung that works.*

## 2. The Postgres work queue (`queue.py`, the `jobs` table)

Stages **enqueue their own work and then claim it back** with `FOR UPDATE SKIP LOCKED`. That one
Postgres primitive means N copies of a process partition the targets with no broker and no
coordinator — each worker atomically grabs a job nobody else holds. Claims carry a lease + heartbeat,
so a crashed worker's job is reaped and re-queued. No Redis, no Celery, no message bus — the database
you already have for your data is also the queue. (Why Postgres for this: see
[`postgres_for_everything.md`](postgres_for_everything.md).)

## 3. The plugin seam (`registry.py`, the `rung.plugins` entry point)

The engine and the CLI are public; domain catalogs plug in. A stage is resolved **by name** through a
string-keyed registry, never by a static import — so the core ships and runs with its heavier stages
**unplugged** (they resolve to stubs that raise `StageNotAvailable` only when invoked), and a separate
package provides the real implementations by registering them under the `rung.plugins` setuptools
entry point. This is what lets the open-source core stay clean while a private overlay (or your own
plugin) supplies proprietary catalogs — the core imports nothing from any overlay, and the boundary is
test-enforced. You don't have to use the seam at all: you can drive the engine directly as a library
(see [`build-your-own-domain.md`](build-your-own-domain.md)).

## 4. The honest-HTTP chokepoint (`http.py`)

**All** network access goes through one session factory, `http.make_session()` — enforced by an AST
guard (`tests/test_http.py`), so nothing constructs a bare client. The published default sends an
honest, self-identifying User-Agent and does **not** spoof a browser fingerprint; TLS impersonation is
opt-in and off by default, so the open-source core circumvents nothing on its own. One chokepoint also
means one place to add polite defaults (rate limiting, retries, proxy routing) for every fetch in
every domain.

---

Together: the **ladder** decides *how* to fetch a target cheaply and resiliently, the **queue**
decides *which* targets each worker takes, the **seam** decides *whose* catalog runs, and the
**chokepoint** makes every fetch honest and consistent. See [`api.md`](api.md) for the exact surface,
and [`../ARCHITECTURE.md`](https://github.com/richard-burhans/rung/blob/main/ARCHITECTURE.md) for the full module map (its "Reusable engine vs the
reference pipeline" section draws the same line between these mechanisms and the cannabis application).
