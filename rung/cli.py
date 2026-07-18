import asyncio
import datetime
from collections.abc import Callable
from typing import Any

import click

from rung import db, registry


def _stage(name: str) -> Callable[..., Any]:
    """Resolve a proprietary stage through the plugin registry (loading plugins on first use).

    The Stage-2/3 scraping catalogs, the comparison intel, and recon live in the private overlay
    and register themselves via the ``rung.plugins`` entry point; the public CLI
    never imports them directly. ``load_plugins`` is idempotent, so calling it per command is
    cheap and keeps ``--help`` import-light (the heavy stack loads only when a command runs).
    With the overlay absent, ``resolve`` returns a stub that raises ``StageNotAvailable`` when
    invoked. See docs/publish_split_design.md.
    """
    registry.load_plugins()
    return registry.resolve(name)


@click.command("recon")
@click.option("--state", default="", help="Limit recon to companies in one state (e.g. PA).")
@click.option(
    "--discover", is_flag=True,
    help="Web-search operators with no derivable homepage to discover their own site "
    "(network, opt-in, fuzzy). Prints a review list to promote into company_homepages.yml.",
)
def recon_cmd(state: str, discover: bool) -> None:
    """Probe company homepages and detect dispensary platform technology."""
    asyncio.run(_run_recon(state=state.strip().upper() or None, discover=discover))


@click.command("fetch-fx")
@click.option(
    "--since", default="",
    help="Backfill start date (YYYY-MM-DD). Default: the earliest priced observation date.",
)
def fetch_fx_cmd(since: str) -> None:
    """Fetch/refresh the daily FX series (CAD->USD, Bank of Canada) for cross-currency price
    normalization. Idempotent; run daily from a cron. See docs/fx_series_design.md."""
    from rung import fx

    start = datetime.date.fromisoformat(since.strip()) if since.strip() else None

    async def _run() -> dict:
        conn = db.get_connection()
        summary = await fx.refresh_fx_rates(conn, since=start)
        conn.close()
        return summary

    summary = asyncio.run(_run())
    if not summary["pairs"]:
        click.echo(f"fetch-fx: {summary['note']}")
        return
    cov = summary["coverage"]
    click.echo(
        f"fetch-fx {', '.join(summary['pairs'])}: {summary['start']} -> {summary['end']} | "
        f"{summary['fetched_business_days']} business days fetched, {summary['days_written']} "
        f"calendar days written"
    )
    click.echo(
        f"  fx_rates CAD/USD coverage: {cov['min']} .. {cov['max']} "
        f"({cov['days']} days, {cov['carried']} carried forward)"
    )


@click.command("bootstrap-dutchie")
@click.option("--state", required=True, help="State to bootstrap from the Dutchie pool (e.g. OK).")
def bootstrap_dutchie_cmd(state: str) -> None:
    """Bootstrap a state's dispensaries roster from the Dutchie pool (for locked-roster states)."""
    bootstrap_dutchie = _stage("bootstrap.dutchie")

    async def _run() -> None:
        conn = db.get_connection()
        pool, inserted, operators = await bootstrap_dutchie(conn, state.strip().upper())
        conn.close()
        click.echo(
            f"Dutchie-pool bootstrap for {state.strip().upper()}: pool={pool} -> "
            f"{inserted} dispensaries across {operators} operators."
        )

    asyncio.run(_run())


@click.command("check-dutchie-hash")
def check_dutchie_hash_cmd() -> None:
    """Report whether Dutchie's ConsumerDispensaries persisted-query hash is still live.

    The directory sweep self-heals past a rotated hash (full-query fallback), so this is a
    health signal, not a hard dependency. Run it from the host/worker egress — from a
    Cloudflare-blocked network every verdict reads as `blocked`. Exit code is 0 only when the
    pinned hash is `live`, so a cron can alert on drift.
    """
    check = _stage("dutchie.hash_health")
    report = asyncio.run(check())
    verdict = report["verdict"]
    persisted, full = report["persisted"], report["full_query"]
    click.echo(f"dutchie ConsumerDispensaries @ {report['anchor']}: {verdict.upper()}")
    click.echo(f"  persisted hash : {persisted['outcome']} ({persisted['count']} stores)")
    click.echo(f"  full-query     : {full['outcome']} ({full['count']} stores)")
    messages = {
        "live": "Pinned hash is current — nothing to do.",
        "stale_hash": "Hash rotated; sweeps run on the full-query fallback. Refresh "
        "CONSUMER_DISPENSARIES_HASH from a live storefront XHR to restore the cheap path.",
        "blocked": "Cloudflare is challenging this egress — re-run from the host/worker.",
        "down": "Neither request style returned data — the endpoint or schema may have changed.",
    }
    click.echo(f"  -> {messages.get(verdict, '')}")
    if verdict != "live":
        raise SystemExit(1)


@click.command("bootstrap-pools")
@click.option("--state", required=True, help="State to additively capture pool stores for (e.g. NY).")
def bootstrap_pools_cmd(state: str) -> None:
    """Additively capture menu-bearing stores from the Dutchie/Weedmaps/Leafly pools.

    For states whose roster uses licensee legal-entity names (NY), the directory sweeps
    can't attribute the pools to those companies, so the menus are lost. This sweeps all
    three pools, names each store by brand, dedupes across pools (richest platform wins),
    and writes them into company_stores under brand companies — leaving the dispensaries
    roster (and so compare-stores) intact. Run dedupe-stores → scrape-menus → compare-stores
    afterward.
    """
    bootstrap_pool_companies = _stage("bootstrap.pools")

    async def _run() -> None:
        conn = db.get_connection()
        stats = await bootstrap_pool_companies(conn, state.strip().upper())
        conn.close()
        click.echo(
            f"Additive pool bootstrap for {state.strip().upper()}: "
            f"pools dutchie={stats['dutchie_pool']} weedmaps={stats['weedmaps_pool']} "
            f"leafly={stats['leafly_pool']} (−{stats['leafly_hemp_dropped']} hemp) -> "
            f"{stats['distinct_physical']} distinct stores across {stats['operators']} "
            f"operators; {stats['stored']} company_stores written."
        )

    asyncio.run(_run())


@click.command("search-states")
@click.option("--failed-only", is_flag=True, help="Only re-search states where the stored URL is dead or was never found.")
@click.option("--force", is_flag=True, help="Skip URL verification and search every state.")
def search_states(failed_only: bool, force: bool) -> None:
    """Search for dispensary program coverage across every states.yml jurisdiction
    (US states + DC + Canadian provinces)."""
    asyncio.run(_run_search_states(failed_only=failed_only, force=force))


@click.command("find-lists")
@click.option("--only", default="", help="Comma-separated state abbrs to limit discovery (e.g. NY,CO).")
@click.option("--force", is_flag=True, help="Re-discover even states that already have a stored list URL.")
def find_lists(only: str, force: bool) -> None:
    """Crawl each state's landing page to find its dispensary-list/locator URL."""
    abbrs = {a.strip().upper() for a in only.split(",") if a.strip()} or None
    asyncio.run(_run_find_lists(only=abbrs, force=force))


@click.command("scrape-states")
@click.option("--only", default="", help="Comma-separated state abbrs to limit extraction (e.g. NY,CO).")
@click.option("--render", is_flag=True, help="Render JS-driven pages in Chrome (pydoll) when static extraction yields nothing.")
@click.option("--ai", is_flag=True, help="Use the AI fallback (needs local Ollama) when static extraction yields nothing.")
@click.option(
    "--record-history", is_flag=True,
    help="Also append store-lifecycle history (state_roster observations into store_locations + "
    "store_observations) alongside each non-empty state replace — the roster leg of the "
    "open/close/acquired time series (cf. scrape-company-stores --record-history).",
)
def scrape_states(only: str, render: bool, ai: bool, record_history: bool) -> None:
    """Extract dispensary records from each state's discovered list URL."""
    abbrs = {a.strip().upper() for a in only.split(",") if a.strip()} or None
    asyncio.run(_run_scrape_states(
        only=abbrs, use_ai=ai, use_render=render, record_history=record_history,
    ))


def _dedupe_state(abbr: str) -> None:
    """Fold shared-brand / cross-listed duplicate stores for one state (``canonical_company_id``).

    Run right after a company-store scrape: dedupe must FOLLOW a scrape, or fragmentation (an operator
    seeded as N per-location companies that each re-scrape the same site) and dangling fold pointers
    accumulate until someone remembers to run ``dedupe-stores`` — the gap that left Ontario's "One Plant"
    as 13 phantom operators across 1,022 store rows.
    """
    from rung.sources.dedupe import print_dedupe_report, run_dedupe
    conn = db.get_connection()
    try:
        report = run_dedupe(conn, abbr)
    finally:
        conn.close()
    print_dedupe_report(report, abbr)


@click.command("scrape-company-stores")
@click.option("--state", default="PA", help="State to scrape company-owned store lists for (e.g. PA).")
@click.option("--ai", is_flag=True, help="Enable the slow AI (Ollama) rung for companies no other method covers.")
@click.option(
    "--only", default="",
    help="Comma-separated terms; scrape only companies whose canonical name contains a term "
    "(case-insensitive) or whose id matches (e.g. 'curaleaf,trulieve') — for a focused re-scrape. "
    "Omit to scrape every company.",
)
@click.option(
    "--remax", is_flag=True,
    help="Re-discovery: walk each company's FULL access-method ladder and keep the "
    "highest-quality rung (menu handles first), instead of the cached cheapest winner — catches "
    "a better rung a thin winner was shadowing. Slow (runs every rung incl. browser/AI); pair "
    "with --only for a targeted refresh.",
)
@click.option(
    "--record-history", is_flag=True,
    help="Also append store-lifecycle history (store_locations + append-only store_observations) "
    "alongside the snapshot — start accumulating the open/close/acquired time series (the store-level "
    "twin of scrape-menus --record-history).",
)
def scrape_company_stores_cmd(
    state: str, ai: bool, only: str, remax: bool, record_history: bool
) -> None:
    """Scrape each company's OWN site for its stores via the access-method registry."""
    abbr = state.strip().upper()
    only_terms = _only_terms(only)
    asyncio.run(_run_company_stores(
        state=abbr, use_ai=ai, only=only_terms, remax=remax, record_history=record_history,
    ))
    # Fold the state's duplicates immediately so fragmentation + dangling folds never accumulate between
    # a scrape and a forgotten `dedupe-stores`. A scoped --only probe stays narrow (it must not disturb
    # the rest of the state, per the pipeline contract), so it does not trigger the full-state fold.
    if not only_terms:
        _dedupe_state(abbr)


@click.command("scrape-menus")
@click.option("--state", default="PA", help="State whose handled stores get their menus scraped (e.g. PA).")
@click.option(
    "--max-age-hours", type=float, default=None,
    help="Only refresh stores whose latest snapshot is older than this many hours "
    "(menus churn daily — e.g. 24 for a daily cron). Omit to scrape every store.",
)
@click.option(
    "--skip-aggregators", is_flag=True,
    help="Skip Weedmaps/Leafly stores entirely. Their menus carry no mg potency and they "
    "dominate the slow per-state tail, so a potency-focused re-scrape finishes far faster.",
)
@click.option(
    "--only-aggregators", is_flag=True,
    help="Scrape ONLY Weedmaps/Leafly stores (mirror of --skip-aggregators) — lets the slow "
    "aggregator tail run as a separate pass from the Dutchie/Jane/etc. stores.",
)
@click.option(
    "--stop-on-cooldown", is_flag=True,
    help="Exit a state's scrape the moment the Weedmaps/Leafly 406 cooldown trips instead of "
    "waiting it out — for long unattended sweeps that shouldn't hang on 10-minute pauses.",
)
@click.option(
    "--only", default="",
    help="Comma-separated terms; scrape only stores whose operator name, store name, or "
    "external_id contains a term (case-insensitive) or whose company id matches "
    "(e.g. 'curaleaf') — for a focused re-scrape. Omit to scrape every handled store.",
)
@click.option(
    "--record-history", is_flag=True,
    help="Also append price/potency/terpene history to the master-DB tables (products + "
    "product_observations, all consumable categories) alongside the snapshot — start accumulating the time "
    "series.",
)
def scrape_menus_cmd(
    state: str, max_age_hours: float | None, skip_aggregators: bool, only_aggregators: bool,
    stop_on_cooldown: bool, only: str, record_history: bool,
) -> None:
    """Scrape each handled store's menu into store_products (Stage 3)."""
    asyncio.run(_run_store_menus(
        state=state.strip().upper(), max_age_hours=max_age_hours,
        skip_aggregators=skip_aggregators, only_aggregators=only_aggregators,
        stop_on_cooldown=stop_on_cooldown, only=_only_terms(only), record_history=record_history,
    ))


@click.command("compare-stores")
@click.option("--state", default="PA", help="State to compare company sites vs the state list (e.g. PA).")
def compare_stores_cmd(state: str) -> None:
    """Diff each operator's own-site stores against the state's dispensary list."""
    run_compare = _stage("compare.run")
    print_compare_report = _stage("compare.print")

    conn = db.get_connection()
    db.create_tables(conn)
    report = run_compare(conn, state.strip().upper())
    conn.close()
    print_compare_report(report)


@click.command("store-lifecycle")
@click.option("--state", default="PA", help="State to derive store lifecycle for (e.g. PA).")
# No default here: the derivation owns it (``store_lifecycle.DEFAULT_CLOSED_AFTER_CYCLES``), and the
# public core cannot import the overlay to mirror the number without drifting from it.
@click.option("--closed-after-cycles", type=int, default=None,
              help="Consecutive missed usable scrape cycles before a store counts as closed [default: 2].")
@click.option(
    "--write", is_flag=True,
    help="Materialize the derived events into store_lifecycle_events (replacing this state's rows) "
    "so maps and the patient UI read them directly instead of re-deriving.",
)
def store_lifecycle_cmd(state: str, closed_after_cycles: int | None, write: bool) -> None:
    """Derive opened/closed/operator-changed events from the append-only store history."""
    run_store_lifecycle = _stage("store_lifecycle.run")
    print_store_lifecycle_report = _stage("store_lifecycle.print")
    tuning = {} if closed_after_cycles is None else {"closed_after_cycles": closed_after_cycles}

    abbr = state.strip().upper()
    conn = db.get_connection()
    db.create_tables(conn)
    report = run_store_lifecycle(conn, abbr, **tuning)
    written = None
    if write:
        written = _stage("store_lifecycle.materialize")(conn, report)
        conn.commit()
    conn.close()
    print_store_lifecycle_report(report)
    if written is not None:
        print(f"\n  wrote {written} row(s) to store_lifecycle_events for {abbr}")


@click.command("dedupe-stores")
@click.option("--state", default="PA", help="State to dedupe company stores for (e.g. PA).")
def dedupe_stores_cmd(state: str) -> None:
    """Collapse shared-brand duplicate stores (e.g. Delta 9 / Keystone IC → Sunnyside)."""
    from rung import queue
    from rung.sources.dedupe import print_dedupe_report, run_dedupe

    abbr = state.strip().upper()
    conn = db.get_connection()
    db.create_tables(conn)

    # One dedupe per state at a time: the clear-then-mark pass reads the whole
    # state and commits once, so a concurrent second run would work from stale rows.
    worker = queue.worker_id()
    queue.requeue_stale(conn, "dedupe")
    queue.enqueue(conn, "dedupe", abbr)
    conn.commit()
    job = queue.claim_target(conn, "dedupe", abbr, worker)
    if job is None:
        holder = queue.live_claim_holder(conn, "dedupe", abbr)
        conn.close()
        click.echo(f"dedupe for {abbr} is already running (claimed by {holder}); exiting.", err=True)
        return

    report = run_dedupe(conn, abbr)
    queue.complete(conn, job.id, "done", worker=worker)
    conn.commit()
    conn.close()
    print_dedupe_report(report, abbr)


@click.command("prune-jobs")
@click.option(
    "--older-than-hours", type=int, default=168,
    help="Delete done/failed jobs finished more than N hours ago (default 168 = 7 days). "
    "Run on a cron after the daily scrape to keep the work-queue claim scans fast.",
)
def prune_jobs_cmd(older_than_hours: int) -> None:
    """Prune finished (done/failed) jobs so the daily store_menu enqueue can't bloat the queue."""
    from rung import queue

    conn = db.get_connection()
    db.create_tables(conn)
    deleted = queue.prune_completed(conn, older_than_hours=older_than_hours)
    conn.commit()
    conn.close()
    click.echo(f"Pruned {deleted} finished jobs older than {older_than_hours}h.")


@click.command("reap-jobs")
def reap_jobs_cmd() -> None:
    """Re-queue lease-expired jobs from crashed workers (the lease-aware reaper).

    Run on a cron (see docs/worker_fleet_deployment.md): any claim whose lease has
    passed — its worker stopped heartbeating — is reset to pending (or failed at the
    attempt cap) so a live worker can pick it up. Complements the per-worker heartbeat
    that live runners now keep automatically.
    """
    from rung import queue

    conn = db.get_connection()
    db.create_tables(conn)
    reaped = sum(
        queue.reap_expired(conn, task_type)
        for task_type in ("store_menu", "company_stores", "dedupe")
    )
    conn.commit()
    conn.close()
    click.echo(f"Reaped {reaped} lease-expired jobs.")


@click.command("worker")
@click.option(
    "--state", required=True,
    help="Comma-separated states this worker drains (e.g. PA,NJ). Jobs are claimed via the "
    "queue (FOR UPDATE SKIP LOCKED), so several workers can run the same states and partition "
    "the targets without a distributed lock — one process per egress IP.",
)
@click.option(
    "--task", type=click.Choice(["menus", "company-stores", "both"]), default="menus",
    help="Which stage's queue to drain (default: menus — the dominant Stage-3 workload).",
)
@click.option(
    "--max-age-hours", type=float, default=24.0,
    help="Menu freshness gate: skip stores snapshotted within this many hours (default 24, a "
    "daily cadence). Ignored for the company-stores stage.",
)
@click.option(
    "--skip-aggregators", is_flag=True,
    help="Skip Weedmaps/Leafly stores — route those to a separate residential-proxied worker.",
)
@click.option(
    "--only-aggregators", is_flag=True,
    help="Drain ONLY Weedmaps/Leafly stores (mirror of --skip-aggregators).",
)
@click.option(
    "--record-history", is_flag=True,
    help="Also append price/potency/terpene history to the master-DB tables (products + "
    "product_observations) alongside the snapshot.",
)
@click.option(
    "--poll-seconds", type=float, default=0.0,
    help="After a drain, re-drain (reaping crashed leases first) every N seconds — a long-lived "
    "fleet worker. 0 (default) drains once and exits, the shape a cron invocation wants.",
)
def worker_cmd(
    state: str, task: str, max_age_hours: float, skip_aggregators: bool,
    only_aggregators: bool, record_history: bool, poll_seconds: float,
) -> None:
    """Run a fleet worker: reap crashed leases, then drain the queue for the given states.

    A first-class entrypoint over the Stage-2/3 runners for distributed deployment (one process
    per egress IP — see docs/worker_fleet_deployment.md). The runners already reap lease-expired
    jobs at startup, keep a per-worker heartbeat, and claim via SKIP LOCKED; this command adds the
    standalone entrypoint, the two-stage combination, and an optional continuous poll loop.
    """
    states = [s.strip().upper() for s in state.split(",") if s.strip()]
    asyncio.run(_run_worker(
        states=states, task=task, max_age_hours=max_age_hours,
        skip_aggregators=skip_aggregators, only_aggregators=only_aggregators,
        record_history=record_history, poll_seconds=poll_seconds,
    ))


@click.command("show-states")
def show_states() -> None:
    """Print the state coverage table from the database."""
    from rung.db import get_all_state_programs
    from rung.sources.state_search import print_report_from_db
    conn = db.get_connection()
    db.create_tables(conn)
    records = get_all_state_programs(conn)
    conn.close()
    if not records:
        click.echo("No state data in database yet. Run search-states first.", err=True)
        return
    print_report_from_db(records)


@click.command("analyze")
@click.argument("url")
@click.option("--save-html", is_flag=True, help="Print instructions for capturing raw HTML.")
def analyze(url: str, save_html: bool) -> None:
    """AI-assisted analysis of a dispensary store-finder page.

    Development tool — requires Ollama running with llama3.2 pulled.
    """
    analyze_url = _stage("analyze")

    analyze_url(url, save_html=save_html)


async def _run_search_states(failed_only: bool = False, force: bool = False) -> None:
    from rung.sources.state_search import print_report, run_state_coverage

    conn = db.get_connection()
    db.create_tables(conn)
    scope = "failed/unchecked states" if failed_only else "all jurisdictions"
    click.echo(f"Checking {scope}…", err=True)
    results = await run_state_coverage(conn, failed_only=failed_only, force=force)
    conn.close()
    print_report(results)


async def _run_find_lists(only: set[str] | None = None, force: bool = False) -> None:
    from rung.sources.state_lists import print_list_report, run_find_lists

    conn = db.get_connection()
    db.create_tables(conn)
    scope = f"{len(only)} states" if only else "all program states"
    click.echo(f"Discovering dispensary-list URLs for {scope}…", err=True)
    results = await run_find_lists(conn, only=only, force=force)
    conn.close()
    print_list_report(results)


async def _run_scrape_states(
    only: set[str] | None = None, use_ai: bool = False, use_render: bool = False,
    record_history: bool = False,
) -> None:
    from rung.sources.extract import (
        print_extract_report,
        run_extract_states,
    )

    conn = db.get_connection()
    db.create_tables(conn)
    scope = f"{len(only)} states" if only else "all states with a discovered list URL"
    click.echo(f"Extracting dispensary records for {scope}…", err=True)
    results = await run_extract_states(
        conn, only=only, use_ai=use_ai, use_render=use_render, record_history=record_history,
    )
    conn.close()
    print_extract_report(results)


def _only_terms(only: str) -> set[str] | None:
    """Parse a comma-separated ``--only`` filter into a set of lowercase terms (None if blank)."""
    terms = {term.strip().lower() for term in only.split(",") if term.strip()}
    return terms or None


async def _run_company_stores(
    state: str, use_ai: bool = False, only: set[str] | None = None, remax: bool = False,
    record_history: bool = False,
) -> None:
    run_company_stores = _stage("company_stores.run")
    print_company_store_report = _stage("company_stores.print")

    conn = db.get_connection()
    db.create_tables(conn)
    scope = f" (only {sorted(only)})" if only else ""
    scope += " [remax]" if remax else ""
    click.echo(f"Scraping company-owned stores for {state}{scope}…", err=True)
    # Persistence happens inside run_company_stores, per claimed job, via the
    # guarded keep-the-best replace (db.replace_company_stores): a flaky low-yield
    # re-run can't clobber good data, and a concurrent run claims other companies.
    results = await run_company_stores(
        conn, state, use_ai=use_ai, only=only, remax=remax, record_history=record_history
    )
    conn.close()
    print_company_store_report(results, state)


async def _run_store_menus(
    state: str, max_age_hours: float | None = None,
    skip_aggregators: bool = False, only_aggregators: bool = False, stop_on_cooldown: bool = False,
    only: set[str] | None = None, record_history: bool = False,
) -> None:
    run_store_menus = _stage("menus.run")
    print_menu_report = _stage("menus.print")

    conn = db.get_connection()
    db.create_tables(conn)
    scope = f" (only {sorted(only)})" if only else ""
    click.echo(f"Scraping store menus for {state}{scope}…", err=True)
    # Persistence happens inside run_store_menus, per claimed job, via the
    # wholesale snapshot replace (empty results keep the prior snapshot).
    results = await run_store_menus(
        conn, state, max_age_hours=max_age_hours,
        skip_platforms={"weedmaps", "leafly"} if skip_aggregators else None,
        only_platforms={"weedmaps", "leafly"} if only_aggregators else None,
        stop_on_cooldown=stop_on_cooldown, only=only, record_history=record_history,
    )
    conn.close()
    print_menu_report(results, state)


async def _run_recon(state: str | None = None, discover: bool = False) -> None:
    run_recon = _stage("recon.run")
    conn = db.get_connection()
    db.create_tables(conn)
    records, discovered = await run_recon(conn, state=state, discover=discover)
    for record in records:
        db.upsert_recon(conn, record)
    conn.commit()
    conn.close()
    scope = f" ({state})" if state else ""
    click.echo(f"Upserted {len(records)} company recon records{scope}.")

    if discover:
        found = [r for r in discovered if r.error is None and r.homepage_url]
        click.echo(
            f"\nHomepage discovery: {len(found)}/{len(discovered)} no-site operators "
            "resolved. Review and promote good ones into data/company_homepages.yml:"
        )
        for record in sorted(found, key=lambda r: r.canonical_name):
            flag = "⚠ " if record.confidence == "low" else "  "
            platform = record.platform or "custom"
            click.echo(f"{flag}{record.canonical_name}: {record.homepage_url}  # {platform}")


async def _run_worker(
    *, states: list[str], task: str, max_age_hours: float, skip_aggregators: bool,
    only_aggregators: bool, record_history: bool, poll_seconds: float,
) -> None:
    """Drain the Stage-2/3 queue for each state, then optionally poll for more.

    Each stage runner reaps lease-expired jobs at startup and claims via SKIP LOCKED, so calling
    them per state per cycle IS the reaper-plus-claim loop; this just sequences the stages and
    (with --poll-seconds) keeps re-draining. Uses one consumer connection for the whole process.
    """
    run_company_stores = _stage("company_stores.run") if task in ("company-stores", "both") else None
    run_store_menus = _stage("menus.run") if task in ("menus", "both") else None

    conn = db.get_connection()
    db.create_tables(conn)
    try:
        while True:
            for abbr in states:
                if run_company_stores is not None:
                    click.echo(f"[worker] draining company-stores for {abbr}…", err=True)
                    results = await run_company_stores(conn, abbr, record_history=record_history)
                    _stage("company_stores.print")(results, abbr)
                if run_store_menus is not None:
                    click.echo(f"[worker] draining menus for {abbr}…", err=True)
                    results = await run_store_menus(
                        conn, abbr, max_age_hours=max_age_hours,
                        skip_platforms={"weedmaps", "leafly"} if skip_aggregators else None,
                        only_platforms={"weedmaps", "leafly"} if only_aggregators else None,
                        record_history=record_history,
                    )
                    _stage("menus.print")(results, abbr)
            if poll_seconds <= 0:
                break
            click.echo(f"[worker] queue drained; re-checking in {poll_seconds:g}s…", err=True)
            await asyncio.sleep(poll_seconds)
    finally:
        conn.close()
