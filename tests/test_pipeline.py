"""Safety-invariant tests for the DB layer and run_extract_states orchestration.

Uses a throwaway Postgres schema and a stubbed extract_records — no network. These
guard the contracts in ARCHITECTURE.md that are easy to regress: idempotent
*non-destructive* replace, and list-column write isolation.
"""

import asyncio

from conftest import pg_conn

from rung import db
from rung.models import DispensaryRecord, StateProgramRecord
from rung.sources import extract


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)
    return conn


def _add_state(conn, abbr, list_url="http://example/list", list_type="html"):
    db.upsert_state_program(conn, StateProgramRecord(
        abbr=abbr, name=abbr, programs="medical", program_term="medical", agency="x"))
    db.set_state_list(conn, abbr, list_url, list_type, "found")
    conn.commit()


def _count(conn, abbr):
    return conn.execute(
        "SELECT COUNT(*) FROM dispensaries WHERE state = %s", (abbr,)
    ).fetchone()[0]


def _run(conn, **kw):
    return asyncio.run(extract.run_extract_states(conn, **kw))


def _stub_extract(monkeypatch, records):
    async def fake(url, list_type):
        # Return fresh copies so the orchestrator can mutate .state freely.
        return [DispensaryRecord(**r.__dict__) for r in records]
    monkeypatch.setattr(extract, "extract_records", fake)


def test_idempotent_replace(monkeypatch):
    conn = _conn()
    _add_state(conn, "ZZ")
    _stub_extract(monkeypatch, [
        DispensaryRecord(source="html", name="A", address="1 St"),
        DispensaryRecord(source="html", name="B", address="2 St"),
    ])
    _run(conn, only={"ZZ"})
    _run(conn, only={"ZZ"})
    assert _count(conn, "ZZ") == 2  # re-run replaces, does not append


def test_preserve_rows_on_empty_extraction(monkeypatch):
    conn = _conn()
    _add_state(conn, "ZZ")
    for nm in ("A", "B", "C"):
        db.insert_dispensary(conn, DispensaryRecord(source="html", name=nm, state="ZZ"))
    conn.commit()

    _stub_extract(monkeypatch, [])  # a dead/empty URL
    results = _run(conn, only={"ZZ"})

    assert _count(conn, "ZZ") == 3  # prior rows preserved, not wiped
    assert results[0].count == 0
    assert results[0].method == "none"


def _stub_partial(monkeypatch, reason="page 3 of 7 failed"):
    async def fake(url, list_type):
        raise extract.PartialRoster(reason)
    monkeypatch.setattr(extract, "extract_records", fake)


def _ai_sentinel(monkeypatch) -> list[str]:
    """Replace the AI tier with a recorder that WOULD install a roster of its own."""
    from rung.sources import ai_fallback

    called: list[str] = []

    async def fake(url, source_tag="ai"):
        called.append(url)
        return [DispensaryRecord(source="ai", name="Hallucinated Store", address="9 Nowhere")]
    monkeypatch.setattr(ai_fallback, "extract_with_ai", fake)
    return called


def test_partial_roster_keeps_rows_skips_ai_and_records_failed(monkeypatch):
    """Audit P-35. A roster refused as a fragment looked merely EMPTY to the later tiers, so with
    `--ai` the AI extractor ran and its rows REPLACED the roster the refusal existed to keep; and
    the attempt was recorded `empty`, which says the source yielded nothing."""
    conn = _conn()
    _add_state(conn, "ZZ")
    for nm in ("A", "B", "C"):
        db.insert_dispensary(conn, DispensaryRecord(source="html", name=nm, state="ZZ"))
    conn.commit()
    _stub_partial(monkeypatch, "page 3 of 7 failed")
    called = _ai_sentinel(monkeypatch)

    results = _run(conn, only={"ZZ"}, use_ai=True, record_history=True)

    assert called == [], "the AI tier ran on a state whose roster was refused, not empty"
    assert _count(conn, "ZZ") == 3
    names = {r[0] for r in conn.execute("SELECT name FROM dispensaries WHERE state = 'ZZ'").fetchall()}
    assert names == {"A", "B", "C"}
    assert (results[0].count, results[0].method) == (0, "none")
    attempt = conn.execute(
        "SELECT outcome, error FROM store_capture_attempts WHERE state = 'ZZ'").fetchall()
    assert attempt == [("failed", "page 3 of 7 failed")]


def test_a_genuinely_empty_roster_still_reaches_the_ai_tier(monkeypatch):
    """ANTI-VACUITY: the gate is specific to a refused fragment. An empty extraction is what the
    AI tier is FOR, and its attempt is recorded under the AI's own result."""
    conn = _conn()
    _add_state(conn, "ZZ")
    _stub_extract(monkeypatch, [])
    called = _ai_sentinel(monkeypatch)

    results = _run(conn, only={"ZZ"}, use_ai=True, record_history=True)

    assert called == ["http://example/list"]
    assert (results[0].count, results[0].method) == (1, "ai")
    attempt = conn.execute(
        "SELECT outcome, error FROM store_capture_attempts WHERE state = 'ZZ'").fetchall()
    assert attempt == [("succeeded", None)]


def test_partial_roster_is_not_offered_to_the_render_tier(monkeypatch):
    """The DEFENSIVE half. No handler that raises `PartialRoster` serves a renderable list_type
    today, so this cannot happen in production — the stub raises from `html` to show the guard
    holds if one ever does. A guard nothing pins is one a refactor removes."""
    conn = _conn()
    _add_state(conn, "ZZ", list_url="http://example/refused")   # list_type "html": renderable
    _add_state(conn, "YY", list_url="http://example/empty")     # an ordinary empty html state
    rendered: list[str] = []

    async def fake_extract(url, list_type):
        if url.endswith("/refused"):
            raise extract.PartialRoster("refused")
        return []

    async def fake_render(renderable, extracted, methods):
        rendered.extend(r.abbr for r in renderable)

    monkeypatch.setattr(extract, "extract_records", fake_extract)
    monkeypatch.setattr(extract, "_render_empty_targets", fake_render)
    _run(conn, only={"ZZ", "YY"}, use_render=True)

    assert rendered == ["YY"], "the empty state is rendered; the refused one never is"


def test_list_columns_not_clobbered_by_state_upsert():
    conn = _conn()
    _add_state(conn, "ZZ", list_url="http://kept/list", list_type="pdf")
    # A later search/verify run upserts the non-list columns.
    db.upsert_state_program(conn, StateProgramRecord(
        abbr="ZZ", name="ZZ", programs="medical", program_term="medical",
        agency="x", best_url="http://landing"))
    conn.commit()

    rec = db.get_state_program(conn, "ZZ")
    assert rec.list_url == "http://kept/list"  # discovered list survives
    assert rec.list_type == "pdf"
    assert rec.best_url == "http://landing"


def test_delete_dispensaries_scoped_to_state():
    conn = _conn()
    db.insert_dispensary(conn, DispensaryRecord(source="html", name="A", state="AA"))
    db.insert_dispensary(conn, DispensaryRecord(source="html", name="B", state="BB"))
    conn.commit()

    deleted = db.delete_dispensaries_for_state(conn, "AA")
    conn.commit()

    assert deleted == 1
    assert conn.execute("SELECT state FROM dispensaries").fetchall() == [("BB",)]


def test_create_tables_omits_companies() -> None:
    """Table ownership (ARCHITECTURE.md contract 3): create_tables creates every table
    EXCEPT `companies`, which seed_companies.py owns. Guards against a regression that
    moves `companies` into db.create_tables."""
    conn = _conn()
    tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
    assert "companies" not in tables
    assert {
        "dispensaries", "company_recon", "company_stores", "access_methods", "state_programs"
    } <= tables
