"""Foreign-exchange rates for cross-currency price normalization.

Prices in the dataset are nominal, each in its store's local currency (USD for US stores, CAD for
Canadian ones — derived from ``state_programs.country``). This module fetches an authoritative daily
FX series so a price can be converted to a common numeraire **at the rate that prevailed on its
observation date** — the reason to hold a series rather than one flat constant, and what makes the
append-only ``product_observations`` series (which spans time) convertible correctly.

**Boundary (read before trusting a converted number).** Spot FX is not purchasing-power parity.
Converting a CAD retail price to USD at the spot rate says what a currency exchange would give, NOT
whether the good is cheaper *in real terms* across two tax-and-regulatory regimes. See
``docs/fx_series_design.md`` §0. The converted view is for descriptive, same-numeraire figures, not
for pooling two markets into one price regression.

Source: the **Bank of Canada Valet** API (official, no key, historical, JSON). Its ``FXUSDCAD``
series is **CAD per USD**; we store **USD per CAD** (base=CAD, quote=USD) so a CAD price converts by
a plain multiply. Valet publishes business days only, so weekends/holidays carry the previous
business day's rate forward, flagged ``is_carried``. A day with no rate to carry forward is left
absent — never fabricated (``reference_db.upsert_fx_rates`` records only real readings).
"""

import datetime
import json

from rung import http, reference_db
from rung.db import DBConn

_BOC_VALET_USDCAD = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
_SOURCE = "bank_of_canada"


async def _fetch_boc_usdcad(start_date: datetime.date) -> list[tuple[datetime.date, float]]:
    """Business-day CAD-per-USD observations from the Bank of Canada, on/after ``start_date``.

    Returns ``(date, cad_per_usd)`` pairs in ascending date order. Raises on a non-200 response so a
    failed fetch surfaces instead of silently writing nothing.
    """
    url = f"{_BOC_VALET_USDCAD}?start_date={start_date.isoformat()}"
    async with http.make_session() as session:
        response = await session.get(url, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            f"Bank of Canada Valet returned HTTP {response.status_code} for {url}"
        )
    payload = json.loads(response.content)
    observations: list[tuple[datetime.date, float]] = []
    # External-data boundary: the payload is untrusted JSON, so guard each field and coerce here.
    for entry in payload.get("observations", []):
        day = entry.get("d")
        value = entry.get("FXUSDCAD", {}).get("v")
        if not day or value in (None, ""):
            continue
        observations.append((datetime.date.fromisoformat(day), float(value)))
    observations.sort()
    return observations


def _forward_fill(
    observations: list[tuple[datetime.date, float]],
    start_date: datetime.date,
    end_date: datetime.date,
) -> list[tuple[datetime.date, str, str, float, str, bool]]:
    """Expand business-day ``(date, cad_per_usd)`` points into one row for **every** calendar day in
    ``[start_date, end_date]``, stored as USD-per-CAD (base=CAD, quote=USD).

    A day the source published is stored as-is (``is_carried=False``); a day it skipped
    (weekend/holiday) carries the last published rate forward (``is_carried=True``). A leading run of
    days before the first available rate is **left absent** — we cannot carry forward from nothing,
    and fabricating a rate would record a self-inflicted gap as a fact about the day.

    Returns rows shaped for :func:`reference_db.upsert_fx_rates`.
    """
    published = dict(observations)
    # Seed the carry from the latest observation strictly before the window, if any.
    prior = [rate for day, rate in observations if day < start_date]
    last_cad_per_usd = prior[-1] if prior else None

    rows: list[tuple[datetime.date, str, str, float, str, bool]] = []
    day = start_date
    step = datetime.timedelta(days=1)
    while day <= end_date:
        todays = published.get(day)
        is_carried = todays is None
        cad_per_usd = last_cad_per_usd if is_carried else todays
        if cad_per_usd is not None:
            usd_per_cad = round(1.0 / cad_per_usd, 6)
            rows.append((day, "CAD", "USD", usd_per_cad, _SOURCE, is_carried))
            last_cad_per_usd = cad_per_usd
        day += step
    return rows


async def refresh_fx_rates(
    conn: DBConn,
    since: datetime.date | None = None,
    today: datetime.date | None = None,
) -> dict:
    """Fetch, forward-fill, and upsert the FX series so every priced observation has a same-day rate.

    ``since`` overrides the backfill start (default: the earliest priced observation date). ``today``
    overrides the end date (tests pass a fixed date). No-ops with a note when the data is US-only.
    Commits. Returns a summary dict for the CLI.
    """
    reference_db.ensure_fx_rates(conn)
    if "CAD" not in reference_db.currencies_needing_conversion(conn):
        return {"pairs": [], "note": "no CAD-priced data present — nothing to fetch"}

    end_date = today or datetime.datetime.now(datetime.UTC).date()
    start_date = since or reference_db.fx_backfill_start(conn) or end_date
    # Fetch a week before the window so a leading run of carried days has a rate to carry forward.
    observations = await _fetch_boc_usdcad(start_date - datetime.timedelta(days=7))
    rows = _forward_fill(observations, start_date, end_date)
    reference_db.upsert_fx_rates(conn, rows)
    conn.commit()

    lo, hi, days, carried = reference_db.fx_rate_coverage(conn, "CAD", "USD")
    return {
        "pairs": ["CAD/USD"],
        "start": start_date,
        "end": end_date,
        "fetched_business_days": len(observations),
        "days_written": len(rows),
        "coverage": {"min": lo, "max": hi, "days": days, "carried": carried},
    }
