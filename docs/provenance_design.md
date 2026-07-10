# Provenance: storing the facts an analysis stands on

Most of what a pipeline knows, it derived. Some of what it *claims* it did not.

An analysis that compares one firm to a market needs to know which brands that firm owns. That fact
is not in the data — it is in a corporate filing. A market-maturity analysis needs to know when a
state's retail sales began. That is in a statute, or a regulator's press release. These are
**premises**: inputs the analysis cannot derive and cannot check.

Left in a script, a premise looks like this:

```python
ACME_BRANDS = r"^(northwind|contoso|acme)$"   # Acme Corp's brands
```

That regex is doing load-bearing work in a published result, and there is no way for a reader — or
for you, in six months — to know where it came from, whether it was ever true, or whether it is
still true. **A premise with no recorded source is unfalsifiable.** Checking it means redoing the
research.

The `attestations` table is where premises live instead.

## The shape

One row is one subject–predicate–object triple, carrying the evidence for itself:

| column | why |
|---|---|
| `subject_type`, `subject`, `predicate`, `object` | the fact: *(brand) Northwind —owned_by→ Acme Corp* |
| `source_type`, `source_ref`, `source_url` | where it came from, citably |
| `quote` | the exact supporting text, so the claim is checkable **in place** |
| `retrieved_at` | **load-bearing.** A brand portfolio is true *of a filing*, not of the world forever |
| `confidence` | `verified` (primary source) · `reported` (reputable secondary) · `inferred` (derived by us) |
| `notes` | the caveat a future reader needs — usually a name collision |

The primary key is the triple **plus `source_ref`**. Two sources may attest the same fact, and a
reader is better served by both than by whichever was written last. If they disagree, the table shows
the disagreement rather than silently resolving it.

```python
from rung import db

db.upsert_attestation(conn, db.Attestation(
    subject_type="brand", subject="Northwind", predicate="owned_by", object="Acme Corp",
    source_type="sec_filing", source_ref="Acme FY2024 Form 10-K",
    source_url="https://www.sec.gov/Archives/edgar/data/…",
    quote="…its portfolio of consumer brands, including Northwind™ and Contoso™…",
    confidence="verified", retrieved_at="2026-07-10",
))

for fact in db.attestations_for(conn, "brand", "northwind"):   # case-insensitive
    print(fact.object, fact.source_ref, fact.retrieved_at)
```

## Three design decisions worth defending

**Negative attestations are first-class.** `not_owned_by` is not a curiosity; it is the failure mode
a brand→producer join actually hits. Consumer brand names collide constantly: a product brand of one
company shares a name with a retail chain owned by another, and plenty of brand names are ordinary
English words. Recording *"Northwind is **not** owned by Acme — it belongs to Contoso Holdings"*
prevents a join that would otherwise look perfectly reasonable and be wrong. Every negative row in a
mature catalog is there because someone nearly made that mistake.

**`confidence` is a grade, not a probability.** It says what *kind* of evidence backs the row, so a
skeptical reader knows where to push. `inferred` is not a slur — it is an invitation.

**The facts are curated in a reviewable file, then loaded.** They live in YAML so that adding one is
a pull request with a source in the diff, and in Postgres so an analysis can `JOIN` against them.
Neither half works alone: a YAML nobody joins against gets stale, and a table nobody reviews gets
wrong.

## Why this is engine infrastructure, not domain data

`attestations` sits with `jobs` and `access_methods` in `create_engine_tables()`, not with the
domain's record tables. The *mechanism* — a fact, its source, its date, its grade — is domain-neutral.
Any pipeline that publishes results built on external premises needs it. The **vocabulary** of
subject types and predicates is the domain's business, and the engine has no opinion about it.

## The property this buys

A result is auditable when a reader can check it with **substantially less effort than it took to
produce**. Re-running an analysis is not that. Reading a claim, following it to a triple, following
the triple to a filing, and reading the quoted sentence — that is.

Recording provenance does not make a premise true. It makes it **checkable**, which is the only
property that has ever distinguished a citation from an assertion.

## See also

- `rung/db.py` — the DDL, `upsert_attestation`, `attestations_for`
- `tests/test_attestations.py` — the properties above, pinned
