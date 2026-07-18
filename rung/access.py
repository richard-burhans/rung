"""Cost-ranked access-method ladder with a persisted, self-healing winner.

Every extraction target — an entity's own site, a directory listing, an index or
landing page — can be reached several ways that differ in expense. This module runs
the cheapest method that works, remembers it per target, and re-walks the ladder only
when that winner fails or goes stale.

Persistence lives in the `access_methods` table
(db.record_access_attempt / get_access_winner).
"""

import asyncio
import datetime
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import psycopg

from rung import db

# A method runner is given the connection, the target key, and a hint carrying the
# resource_url/params that worked last time (both None on first run). It returns the
# records it found plus the concrete resource_url/params it used, so the winner is
# remembered with the exact locator — not just the method name.
MethodHint = tuple[str | None, dict | None]
MethodResult = tuple[list, str | None, dict | None]
MethodRunner = Callable[[db.DBConn, str, MethodHint], Awaitable[MethodResult]]

# ── the outcome vocabulary ────────────────────────────────────────────────────────────────────────
#
# A rung that returns nothing has said almost nothing. There are four reasons it might, and they call
# for four different responses:
#
#   Unavailable  the WORLD says no. This target has no data by this route. Nothing to fix.
#   Blocked      we were REFUSED. The data exists; this egress cannot have it. Rotate, back off, wait.
#   Broken       WE are wrong. A dead URL, a changed payload, missing config. Fix the rung.
#   (silence)    the rung returned nothing and did not say why → recorded as 'failed'.
#
# Collapsing these is this project's signature bug. Three times, a failure of OURS was persisted and
# reported as a fact about the world: Unpaywall 422'd on a placeholder email and the paper was called
# "paywalled"; a Europe PMC URL 404'd for *every* article and the papers were called "paywalled"; NCBI
# returned HTTP 200 with a reCAPTCHA page, which a naive status check would have called a success.
# Two of the three were invisible for weeks, because a green pipeline and a comfortable sentence agree.
#
# The asymmetry is deliberate: **only an explicit Unavailable produces 'unavailable'.** A rung that is
# merely silent is 'failed' — unknown — because the cost of mistaking a broken rung for an empty world
# is that you stop looking.


class MethodOutcome(Exception):
    """Base for a runner's explanation of why it produced no records. Carries its ``status``."""

    status = "failed"


class Unavailable(MethodOutcome):
    """The world says no: this target has no data by this route, and re-trying will not change that.

    Raise this only when the SOURCE told you so — a documented "not open access" verdict, a 404 from
    an API that is authoritative for the question, an empty result from a complete listing. Never
    raise it because a request failed.
    """

    status = "unavailable"


class Blocked(MethodOutcome):
    """We were refused: 403/429/captcha/soft-block. The data exists; this egress cannot have it."""

    status = "blocked"


class Broken(MethodOutcome):
    """We are wrong: a dead URL, a changed payload shape, a missing credential. Fix the rung.

    A rung that raises this is asking to be repaired. It should be loud in a canary and never be
    mistaken for the target simply having no data.
    """

    status = "broken"


@dataclass(frozen=True)
class AccessMethod:
    """One extraction method for a target_type.

    ``cost_rank`` is **try-priority** (lower = tried first): cost-informed, but a
    self-gating high-confidence method may be ranked into the cheap band so its complete
    result wins before a noisy generic rung shadows it ("quality vs cheapest"). The catalog
    is sorted by it in ``run_target``.
    """

    name: str
    cost_rank: int
    run: MethodRunner


def is_plausible(record: object) -> bool:
    """A record counts if it has a name and at least one location signal.

    The default plausibility test, shaped for STORE records; target types whose
    records look different (e.g. menu products) pass their own predicate to
    ``run_target``.
    """
    if not getattr(record, "name", None):
        return False
    return any(
        getattr(record, field, None) for field in ("address", "city", "zip_code")
    )


def is_success(
    records: list,
    min_records: int = 1,
    plausible: Callable[[object], bool] = is_plausible,
) -> bool:
    return sum(1 for record in records if plausible(record)) >= min_records


def _age_days(last_ok_at: str | None) -> float | None:
    """Days since the winner last succeeded, or None if it never has."""
    if last_ok_at is None:
        return None
    last_ok = datetime.datetime.fromisoformat(last_ok_at)
    return (datetime.datetime.now(datetime.UTC) - last_ok).total_seconds() / 86400.0


def _host_of(resource_url: str | None) -> str:
    """The host a re-walk would hit, used to rate-limit per site. '' if unknown."""
    if not resource_url:
        return ""
    return urlparse(resource_url).netloc.removeprefix("www.")


@dataclass
class ReExploreGovernor:
    """RED-inspired admission control for *discretionary* staleness re-walks.

    Inspired by Random Early Detection, not a literal port. Two gates spread the
    load: a temporal ramp de-synchronizes a same-day cohort across runs, and a
    per-host limiter keeps any single site (including a shared platform host) from
    being hammered within a run. Failure re-walks bypass this entirely. The object
    holds per-run, per-host state, so create one per batch and share it.
    """

    min_days: float = 25.0
    max_days: float = 35.0
    max_age_prob: float = 0.6
    host_soft: int = 3          # re-walks per host before throttling begins
    host_hard: int = 8          # never admit more than this per host per run
    rand: Callable[[], float] = random.random
    _host_walks: dict[str, int] = field(default_factory=dict)

    def _age_prob(self, age_days: float) -> float:
        if age_days < self.min_days:
            return 0.0
        if age_days >= self.max_days:
            return 1.0
        span = self.max_days - self.min_days
        return self.max_age_prob * (age_days - self.min_days) / span

    def _host_factor(self, host: str) -> float:
        walked = self._host_walks.get(host, 0)
        if walked >= self.host_hard:
            return 0.0
        if walked >= self.host_soft:
            # Throttling begins once host_soft full walks are spent (the +1 makes
            # the host_soft-th walk already throttled, not the host_soft+1-th).
            return 1.0 - (walked - self.host_soft + 1) / (self.host_hard - self.host_soft + 1)
        return 1.0

    def admit(self, age_days: float, host: str) -> bool:
        """Decide whether to re-explore now; counts the host walk if admitted."""
        probability = self._age_prob(age_days) * self._host_factor(host)
        if probability <= 0.0:
            return False
        if self.rand() < probability:
            self._host_walks[host] = self._host_walks.get(host, 0) + 1
            return True
        return False


def _hint_for(row: tuple | None) -> MethodHint:
    """Build a (resource_url, params) hint from a stored access_methods row."""
    if row is None:
        return None, None
    resource_url = row[db.ACCESS_METHOD_COLUMNS.index("resource_url")]
    params_json = row[db.ACCESS_METHOD_COLUMNS.index("params")]
    params = json.loads(params_json) if params_json else None
    return resource_url, params


async def _attempt(
    method: AccessMethod, conn: db.DBConn, target_key: str, hint: MethodHint
) -> tuple[list, str | None, dict | None, MethodOutcome | None]:
    """Run one rung. Returns its result, or the outcome it raised to explain producing nothing.

    A CRASHING RUNG IS A `Broken` RUNG — IT IS NOT A SILENT TARGET.

    This used to let an unexpected exception propagate, on the reasoning that a rung which crashes has
    not told us why it failed and swallowing it would recreate the very silence this vocabulary exists
    to break. The reasoning was right and the code did the opposite, because `run_target` has exactly two
    callers and BOTH wrap it in a bare `except Exception: return None, []` — so the propagated crash was
    caught one frame up, dropped on the floor, and the target ended the run with **no `access_methods`
    row at all**: not broken, not failed, nothing. It read as *this target has no method* — the precise
    mistake the comment at the top of this file says has bitten the project three times. And the ladder
    below the crashing rung never ran: one shape-changed payload in a CHEAP rung silently disabled every
    EXPENSIVE rung that would have worked.

    So we name it. An exception a rung did not raise deliberately means WE are wrong — a dead URL, a
    changed payload shape, a missing credential — which is `Broken`'s definition. It is recorded as
    `broken` (surfacing in `access_health`), and the ladder CONTINUES to the next rung.

    `CancelledError` still propagates: that is the caller shutting us down, not a rung failing.
    """
    try:
        records, resource_url, params = await method.run(conn, target_key, hint)
    except MethodOutcome as outcome:
        return [], None, None, outcome
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The rung may have left the connection in an ABORTED transaction (it holds `conn` for its hint
        # lookups), and the caller is about to write an attempt row on it — which would itself fail. So
        # reset it. But ONLY when it is actually poisoned: an unconditional `conn.rollback()` here also
        # discards the CALLER's uncommitted work, which is not ours to throw away. (The first draft of
        # this did exactly that, and the test below caught it by losing its own `CREATE TABLE`.)
        if conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
            conn.rollback()
        return [], None, None, Broken(f"{type(exc).__name__}: {exc}")
    return records, resource_url, params, None


async def run_target(
    conn: db.DBConn,
    target_type: str,
    target_key: str,
    catalog: list[AccessMethod],
    *,
    min_records: int = 1,
    governor: ReExploreGovernor | None = None,
    plausible: Callable[[object], bool] = is_plausible,
    max_yield: bool = False,
    quality_key: Callable[[list], Any] = len,
) -> tuple[str | None, list]:
    """Run the ladder for one target, returning (winning_method, records).

    A stored winner is tried first and, on success, the cheaper rungs are skipped.
    The full ladder is walked cheapest-first instead when there is no winner, when
    the winner fails (mandatory re-walk), or when the governor admits a discretionary
    staleness re-walk. With no governor, a working winner is trusted indefinitely.
    Persists every attempt and commits per attempt.

    With ``max_yield=True`` the ladder is walked in FULL (no winner short-circuit) and the
    **highest-QUALITY** successful rung wins instead of the first success — ranked by
    ``quality_key`` (applied to each rung's plausible records; ties go to the cheaper rung).
    This is a discretionary "best data now" mode for re-discovery passes, where a cheap rung
    returning a thin result would otherwise lock out a richer one. ``quality_key`` defaults to
    ``len`` (raw count), but a caller should pass a key that captures real quality — e.g.
    menu-handle-bearing rows first, so a large bare-address aggregator sweep can't out-rank a
    smaller first-party list with menu handles (which the keep-the-best replace would then
    wrongly block). It does not change the persisted winner contract (``get_access_winner``
    still returns the cheapest 'ok' rung for future cheapest-first runs); the richer result is
    realized in this call's return value. The cheapest-first default is unchanged.
    """
    ladder = sorted(catalog, key=lambda method: method.cost_rank)
    rows = {row[0]: row for row in db.get_access_methods(conn, target_type, target_key)}

    if max_yield:
        best: tuple[str, list, Any] | None = None
        for method in ladder:
            records, resource_url, params, outcome = await _attempt(
                method, conn, target_key, _hint_for(rows.get(method.name))
            )
            if outcome is not None:
                db.record_access_attempt(
                    conn, target_type, target_key, method.name, method.cost_rank,
                    outcome.status, error=str(outcome) or outcome.status,
                )
                conn.commit()
                continue
            kept = [record for record in records if plausible(record)]
            if len(kept) >= min_records:
                db.record_access_attempt(
                    conn, target_type, target_key, method.name, method.cost_rank,
                    "ok", resource_url, json.dumps(params) if params else None, len(records),
                )  # full fetched count (matches the cheapest-first path + design doc); diagnostic only (L-26)
                # Strictly-greater so a tie keeps the earlier (cheaper) rung.
                score = quality_key(kept)
                if best is None or score > best[2]:
                    best = (method.name, records, score)
            else:
                db.record_access_attempt(
                    conn, target_type, target_key, method.name, method.cost_rank,
                    "failed", error="no_plausible_records",
                )
            conn.commit()
        return (best[0], best[1]) if best is not None else (None, [])

    winner = db.get_access_winner(conn, target_type, target_key)
    order = ladder
    # The stored winner may no longer be in this run's catalog (e.g. `ai_llm` won on a
    # `--ai` run but `--ai` is off now) — only winner-first reorder when it's actually
    # present, else fall through to the plain cheapest-first ladder.
    catalog_names = {method.name for method in ladder}
    if winner is not None and winner[0] in catalog_names:
        winner_name = winner[0]
        winner_rank = winner[db.ACCESS_METHOD_COLUMNS.index("cost_rank")]
        resource_url = winner[db.ACCESS_METHOD_COLUMNS.index("resource_url")]
        last_ok_at = winner[db.ACCESS_METHOD_COLUMNS.index("last_ok_at")]
        age = _age_days(last_ok_at)
        re_explore = (
            governor is not None
            and age is not None
            and governor.admit(age, _host_of(resource_url))
        )
        # A NEW rung added to the catalog below the winner's cost has no attempt
        # row yet — walk cheapest-first once so it gets its shot (the winner still
        # runs later in the walk if the newcomer yields nothing). Without this, a
        # working winner would shadow a cheaper/better method forever.
        untried_cheaper = any(
            method.cost_rank < winner_rank and method.name not in rows
            for method in ladder
        )
        if not re_explore and not untried_cheaper:
            order = [m for m in ladder if m.name == winner_name] + [
                m for m in ladder if m.name != winner_name
            ]

    for method in order:
        records, resource_url, params, outcome = await _attempt(
            method, conn, target_key, _hint_for(rows.get(method.name))
        )
        if outcome is not None:
            # The rung said WHY. Persist that, and keep walking — a target that is `unavailable` by
            # one route may still be reachable by another, and the next canary run can tell the
            # difference between a source that has nothing and a rung that is broken.
            db.record_access_attempt(
                conn, target_type, target_key, method.name, method.cost_rank,
                outcome.status, error=str(outcome) or outcome.status,
            )
            conn.commit()
            continue
        if is_success(records, min_records, plausible):
            db.record_access_attempt(
                conn, target_type, target_key, method.name, method.cost_rank,
                "ok", resource_url, json.dumps(params) if params else None,
                len(records),
            )
            conn.commit()
            return method.name, records
        db.record_access_attempt(
            conn, target_type, target_key, method.name, method.cost_rank,
            "failed", error="no_plausible_records",
        )
        conn.commit()
    return None, []
