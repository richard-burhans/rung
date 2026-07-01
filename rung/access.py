"""Cost-ranked access-method ladder with a persisted, self-healing winner.

Every extraction target — a company's stores, a state's dispensary list, a state
agency's landing page — can be reached several ways that differ in expense. This
module runs the cheapest method that works, remembers it per target, and re-walks
the ladder only when that winner fails or goes stale.

Persistence lives in the `access_methods` table
(db.record_access_attempt / get_access_winner).
"""

import datetime
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from rung import db

# A method runner is given the connection, the target key, and a hint carrying the
# resource_url/params that worked last time (both None on first run). It returns the
# records it found plus the concrete resource_url/params it used, so the winner is
# remembered with the exact locator — not just the method name.
MethodHint = tuple[str | None, dict | None]
MethodResult = tuple[list, str | None, dict | None]
MethodRunner = Callable[[db.DBConn, str, MethodHint], Awaitable[MethodResult]]

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
            records, resource_url, params = await method.run(
                conn, target_key, _hint_for(rows.get(method.name))
            )
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
        records, resource_url, params = await method.run(
            conn, target_key, _hint_for(rows.get(method.name))
        )
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
