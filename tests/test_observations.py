"""Tests for the master-product observation history — fingerprint identity + append-only write path
(rung.text.product_fingerprint, db.record_observations)."""

import datetime

from conftest import pg_conn

from rung import db, text
from rung.models import StoreProductRecord

_DAY1 = datetime.datetime(2026, 6, 25, 10, 0, tzinfo=datetime.UTC)
_DAY2 = datetime.datetime(2026, 6, 26, 10, 0, tzinfo=datetime.UTC)


def _flower(
    name: str = "Blue Dream", *, brand: str = "STIIIZY", size_g: float | None = 3.5,
    price: float | None = 40.0, thc: float | None = 25.0, cbd: float | None = None,
    thc_mg: float | None = None, cbd_mg: float | None = None,
    terpenes_std: dict | None = None, terp_total: float | None = None,
    cannabinoids_std: dict | None = None,
    category_std: str = "Flower", product_type_std: str | None = "Bud",
) -> StoreProductRecord:
    return StoreProductRecord(
        company_id=1, state="CA", store_key="dutchie:s1", platform="dutchie", external_id="s1",
        source="dutchie_products", name=name, brand=brand, category_std=category_std,
        product_type_std=product_type_std, size_g=size_g, price=price, thc=thc, cbd=cbd,
        thc_mg=thc_mg, cbd_mg=cbd_mg,
        terpenes_std=terpenes_std, terp_total=terp_total, cannabinoids_std=cannabinoids_std,
    )


def _conn() -> db.DBConn:
    conn = pg_conn()
    db.create_tables(conn)  # creates products + product_observations among the rest
    return conn


def _obs_count(conn: db.DBConn) -> int:
    return conn.execute("SELECT count(*) FROM product_observations").fetchone()[0]


def test_product_fingerprint_identity_excludes_potency() -> None:
    a = text.product_fingerprint("STIIIZY", "Blue Dream", 3.5, "Bud")
    # case- and whitespace-insensitive on name + brand → same identity
    assert text.product_fingerprint("stiiizy", "  blue   dream ", 3.5, "Bud") == a
    # potency/terpenes are NOT part of identity (they're batch-variable observations)
    assert a is not None
    # but size / type / brand / name ARE
    assert text.product_fingerprint("STIIIZY", "Blue Dream", 7.0, "Bud") != a
    assert text.product_fingerprint("STIIIZY", "Blue Dream", 3.5, "Pre-Roll") != a
    assert text.product_fingerprint("Cookies", "Blue Dream", 3.5, "Bud") != a
    assert text.product_fingerprint("STIIIZY", "OG Kush", 3.5, "Bud") != a
    # no name → no identity
    assert text.product_fingerprint("STIIIZY", "", 3.5, "Bud") is None


def test_product_fingerprint_is_dose_aware_for_mg_products() -> None:
    base = ("Kiva", "Camino Gummies", None, "Gummies")  # edible: no size_g
    # same name/brand/type but different manufactured dose → DIFFERENT identities
    f10 = text.product_fingerprint(*base, thc_mg=10.0)
    f100 = text.product_fingerprint(*base, thc_mg=100.0)
    assert f10 is not None and f100 is not None and f10 != f100
    # cbd dose also disambiguates (1:1 vs THC-only at the same THC dose)
    assert text.product_fingerprint(*base, thc_mg=10.0, cbd_mg=10.0) != f10
    # backward-compatible: with no dose, the hash is identical to the 4-arg v1 (weight-sold path)
    flower = ("STIIIZY", "Blue Dream", 3.5, "Bud")
    assert text.product_fingerprint(*flower) == text.product_fingerprint(*flower, thc_mg=None)


def test_record_observations_separates_edibles_by_dose() -> None:
    conn = _conn()
    ten = _flower(name="Camino", brand="Kiva", size_g=None, thc_mg=10.0,
                  category_std="Edible", product_type_std="Gummies")
    hundred = _flower(name="Camino", brand="Kiva", size_g=None, thc_mg=100.0,
                      category_std="Edible", product_type_std="Gummies")
    assert db.record_observations(conn, "dutchie:s1", [ten, hundred], now=_DAY1) == 2
    # two distinct master products despite identical name/brand/type — the dose disambiguates
    assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 2


def test_record_observations_skips_non_consumable_categories() -> None:
    conn = _conn()
    # Accessory / Other / uncategorized carry no chemistry → not observed
    accessory = _flower(category_std="Accessory", product_type_std=None)
    assert db.record_observations(conn, "dutchie:s1", [accessory]) == 0
    assert _obs_count(conn) == 0
    # but a consumable (vape) IS observed now that the scope is widened beyond flower
    vape = _flower(category_std="Vape", product_type_std="Cartridge")
    assert db.record_observations(conn, "dutchie:s1", [vape]) == 1
    assert _obs_count(conn) == 1


def test_record_observations_appends_then_skips_unchanged_same_day() -> None:
    conn = _conn()
    record = _flower()
    assert db.record_observations(conn, "dutchie:s1", [record], now=_DAY1) == 1  # first → append
    assert db.record_observations(conn, "dutchie:s1", [record], now=_DAY1) == 0  # same vals/day → skip
    assert _obs_count(conn) == 1
    assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 1


def test_record_observations_appends_on_value_change() -> None:
    conn = _conn()
    assert db.record_observations(conn, "dutchie:s1", [_flower(price=40.0)], now=_DAY1) == 1
    assert db.record_observations(conn, "dutchie:s1", [_flower(price=35.0)], now=_DAY1) == 1  # price↓
    assert db.record_observations(conn, "dutchie:s1", [_flower(thc=27.0)], now=_DAY1) == 1    # potency
    assert _obs_count(conn) == 3
    # still ONE master product — potency/price are observations, not identity
    assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 1


def test_record_observations_appends_on_cannabinoid_change() -> None:
    conn = _conn()
    assert db.record_observations(conn, "dutchie:s1", [_flower(cannabinoids_std={"CBG": 1.0})], now=_DAY1) == 1
    # identical minor-cannabinoid value, same day → skip (no duplicate heartbeat)
    assert db.record_observations(conn, "dutchie:s1", [_flower(cannabinoids_std={"CBG": 1.0})], now=_DAY1) == 0
    # a changed minor-cannabinoid value → new observation (only price/potency/terps/cannabinoids differ)
    assert db.record_observations(conn, "dutchie:s1", [_flower(cannabinoids_std={"CBG": 1.4})], now=_DAY1) == 1
    assert _obs_count(conn) == 2
    stored = conn.execute(
        "SELECT cannabinoids_std FROM product_observations ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert stored == {"CBG": 1.4}


def test_record_observations_daily_heartbeat_when_unchanged() -> None:
    conn = _conn()
    assert db.record_observations(conn, "dutchie:s1", [_flower()], now=_DAY1) == 1
    # identical values but a NEW day → one presence heartbeat row
    assert db.record_observations(conn, "dutchie:s1", [_flower()], now=_DAY2) == 1
    assert _obs_count(conn) == 2
