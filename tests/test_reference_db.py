from rung import reference_db
from tests.conftest import pg_conn


def test_natural_flower_guard_columns_exist_on_every_table_it_is_aliased_onto() -> None:
    """The check that would have caught the `products` migration gap (2026-07-17).

    `natural_flower_where(alias)` is aliasable **precisely so it can be used on the `products` master**
    (`thc_inflation_by_jurisdiction.py`'s history half does `natural_flower_where('p')`). But `products`
    is the ONE domain table with no `_migrate_*` function, so when the obtention facet (#293) added
    `obtention_std` it had nowhere to land on `products` — the master silently stayed behind and the
    history analysis died with `UndefinedColumn: column p.obtention_std does not exist`.

    A guard that names a column is a contract that the column exists everywhere the guard is used. This
    test builds the real reference schema and asserts exactly that, so the seam cannot rot again.
    """
    conn = pg_conn()
    reference_db.create_reference_tables(conn)
    # The columns the aliasable natural-flower guard depends on, and every table it is legitimately
    # aliased onto in the analysis scripts (bare = store_products, 'p' = products master).
    guard_columns = ("category_std", "obtention_std")
    for table in ("store_products", "products"):
        cols = {
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s",
                (table,),
            ).fetchall()
        }
        missing = [c for c in guard_columns if c not in cols]
        assert not missing, (
            f"`{table}` is missing {missing}, but `natural_flower_where` is aliased onto it. "
            "A table the guard names must carry the guard's columns — add them to its DDL and "
            "`_migrate_*`."
        )


def test_every_added_columns_constant_has_a_wired_migration() -> None:
    """`products` fell behind because it had an `ADDED_COLUMNS`-shaped need but no `_migrate_*` wired in.

    Enumerate the invariant instead of trusting memory: every `_*_ADDED_COLUMNS` constant must have a
    matching `_migrate_*` function, and that function must be CALLED from `create_reference_tables` —
    a defined-but-uncalled migration is the same silent gap with an extra step.
    """
    import inspect

    source = inspect.getsource(reference_db.create_reference_tables)
    for name in dir(reference_db):
        if not name.endswith("_ADDED_COLUMNS"):
            continue
        # _STORE_PRODUCT_ADDED_COLUMNS -> _migrate_store_products (pluralise the singular table stem).
        stem = name[len("_"):-len("_ADDED_COLUMNS")].lower()
        migrate = f"_migrate_{stem}s" if not stem.endswith("s") else f"_migrate_{stem}"
        assert hasattr(reference_db, migrate), (
            f"{name} exists but {migrate}() does not — the column has nowhere to land on an older DB "
            "(this is exactly how `products.obtention_std` was missed)."
        )
        assert f"{migrate}(conn)" in source, (
            f"{migrate}() is defined but not called from create_reference_tables — an uncalled "
            "migration never runs against a live database."
        )


def test_trusted_lineage_where_excludes_pre_fix_weedmaps() -> None:
    """Weedmaps' lineage was a DEFAULTED field — every product came back "Indica".

    Pre-fix Weedmaps scrapes read 44.7-64.6% "Indica" (97.2% on flower) against a Hybrid-majority
    market; the one post-fix scrape reads 28.0%, exactly matching Dutchie. 564,801 rows are
    contaminated, and gating on `strain_type_std IS NOT NULL` — the obvious check — does not catch
    any of them. It let a false finding reach a paper draft's abstract (E1/#105, retracted).

    So the guard names the platform AND the fix date. This test exists so nobody "simplifies" it back
    into a NULL check.
    """
    from rung.reference_db import TRUSTED_LINEAGE_WHERE

    assert "strain_type_std IS NOT NULL" in TRUSTED_LINEAGE_WHERE
    assert "weedmaps" in TRUSTED_LINEAGE_WHERE, "the guard must exclude the contaminated platform"
    assert "scraped_at <" in TRUSTED_LINEAGE_WHERE, "…and only the rows scraped BEFORE the fix"


def test_no_script_retypes_the_natural_flower_guard() -> None:
    """THREE live analyses had hand-rolled copies of the flower filter, and all three had DRIFTED.

    `indica_sativa_consumer.py`, `chemovar_history_pilot.py` and `thc_inflation_by_jurisdiction.py` each
    re-typed the predicate — because the constant was UNALIASED and their queries needed `sp.`/`p.`-prefixed
    columns. Every copy kept the OLD, SHORT regex, so all three silently admitted
    `cartridge | pods | syringe | distillate | rosin | shatter | wax | dabs | powder` rows — CONCENTRATES
    MISLABELLED AS FLOWER — one of them into the labelled-THC inflation series D2 rests on.

    A constant that cannot be used in the query you have is a constant that will be retyped. The fix was to
    make it an aliasable FUNCTION (`db.natural_flower_where(alias)`), which removes the *motive*. This test
    removes the *opportunity*.

    **2026-07-16: the regex this guards against is now retired entirely.** `NATURAL_FLOWER_WHERE` is
    `category_std = 'Flower' AND obtention_std IS NULL` — the name-tells moved to the `obtention_std`
    facet, stamped at `menu_extractors._record` (reports/obtention_facet_design.md). The guard composes
    now, so the motive is gone for good. This test stays because the *shape* of the mistake outlives the
    particular regex: any hand-rolled flower predicate is the bug, whatever it is spelled with.
    """
    import re
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    # The ONE legitimate holder of the retired spelling: it exists to DIFF the new facet against the old
    # regex (`--verify`), which is how the port was validated at all — it found `rosin` matching inside
    # "3 B-ROS-IN-door" and `powder` inside "Gunpowder". It dies when the verification is retired.
    ALLOWED = {"backfill_obtention.py"}
    copy = re.compile(r"category_std\s*=\s*'Flower'\s+AND\s+\S*product_type_std\s+IS\s+DISTINCT\s+FROM",
                      re.IGNORECASE)
    offenders = [
        f"{f.name}: {line.strip()[:80]}"
        for f in sorted(scripts.glob("*.py"))
        if f.name not in ALLOWED
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines()
        if not line.lstrip().startswith("#") and copy.search(line)
    ]
    assert not offenders, (
        "a script re-typed the natural-flower filter instead of calling `db.natural_flower_where(alias)`. "
        "Every previous copy DRIFTED and admitted concentrates into a flower analysis:\n  "
        + "\n  ".join(offenders)
    )


def test_leafly_potency_is_guarded() -> None:
    """Leafly's `thc` is the FOURTH manufactured-value contamination, and it is in published findings.

    On natural flower, **13.29% of Leafly rows claim an impossible >= 40% THC — 21x Dutchie's 0.62%.**
    Leafly is 8.4% of natural-flower rows and supplies **60% of the entire impossible tail**; excluding it,
    the corpus-wide impossible share falls 1.85% -> 0.81%. It is not our parser (the extractor matches a
    single percent and cannot double anything) and it is DIFFERENTIAL BY STATE — which is fatal, because
    state is the unit of analysis in the potency-ceiling and inflation-by-jurisdiction work.

    The guard names the platform, exactly as `TRUSTED_LINEAGE_WHERE` does for Weedmaps' lineage. This test
    exists so nobody "simplifies" it away.
    """
    from rung.reference_db import TRUSTED_POTENCY_WHERE, trusted_potency_where

    assert "leafly" in TRUSTED_POTENCY_WHERE
    assert trusted_potency_where("p").startswith("p.platform")
    assert trusted_potency_where() == TRUSTED_POTENCY_WHERE


def test_potency_headline_scripts_apply_the_leafly_guard() -> None:
    """The test above asserts the guard is SPELLED right. It never asserted anyone USED it — and nobody did.

    On 2026-07-17, **0 of 17 `conference_*.py` scripts referenced the guard at all**, though the CSHL
    abstract's potency headlines come from them and `potency_ceiling.py` — the analysis the guard was
    built for — has always applied it. `thc_inflation_by_jurisdiction.py` was unguarded too, despite being
    named in the guard's own docstring as the second place where state is the unit of analysis.

    That is the H1 shape this repo keeps re-learning: **an invariant enforced in the module that defines it
    and defeated at its callers.** A test that checks the constant protects the definition, not the claim.

    Scope note — why this list is explicit and short rather than "every script touching `thc`". 39 of the
    40 scripts reading `thc` off `store_products` do not reference the guard, and MOST OF THEM ARE RIGHT
    NOT TO: the ETL/export scripts (`backfill_*`, `export_products`, `refresh_product_latest`) must not
    silently drop a platform from the dataset, and the terpene-gated analyses exclude Leafly *implicitly*
    because **Leafly publishes zero terpene rows** — the guard there is a no-op, and adding it would be
    five edits that change nothing while implying they changed something. So this test names the scripts
    whose HEADLINE is a potency statistic over a pool Leafly actually reaches. Adding one is a deliberate
    act; that is the point. (`consistency_audit`'s lesson: ask what a reader DOES with a false positive.)
    """
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    # Scripts whose headline is a potency statistic over a pool Leafly reaches (i.e. not terpene-gated).
    MUST_GUARD = {
        "potency_ceiling.py",
        "thc_inflation_by_jurisdiction.py",
        "conference_producer_potency.py",
        "conference_scope_contrast.py",
        "conference_thc_price.py",
        "conference_tier1_figs.py",
        "conference_sample_selection.py",
    }
    missing = sorted(
        name for name in MUST_GUARD
        if "trusted_potency_where" not in (scripts / name).read_text(encoding="utf-8", errors="replace")
    )
    assert not missing, (
        "a potency-headline script dropped `db.trusted_potency_where()`. Leafly's `thc` is manufactured "
        "upstream, supplies 60% of the impossible tail, and is DIFFERENTIAL BY STATE — unguarded, these "
        "report a platform's mix as a jurisdiction's culture:\n  " + "\n  ".join(missing)
    )
