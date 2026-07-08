# Contributing

Thanks for your interest. This repo is the **open-source core** of `rung` — a cost-ranked
web-scraping framework: the access-method engine, the work queue, the Postgres layer, the CLI,
generic roster extractors, and a plugin seam. Per-domain scraping catalogs, recipes, and datasets
are *not* part of this repo; they plug in through an entry point (see
[Extending the framework](#extending-the-framework-write-a-plugin) below). The framework's first and
reference application is a cannabis-dispensary dataset, but the engine is domain-agnostic — see
[`docs/build-your-own-domain.md`](docs/build-your-own-domain.md) to build a pipeline for your own
targets. So there are two ways to contribute:

- **Improve the core** — the engine, the extractors, the CLI, docs, tests.
- **Build your own plugin** — provide the proprietary stages for your own targets, against the
  public seam, in your own package. You don't need to touch this repo to do that.

Start with [`ARCHITECTURE.md`](ARCHITECTURE.md) for the module map and the cross-cutting contracts.

## Development setup

- **Python ≥ 3.13**, managed with [`uv`](https://docs.astral.sh/uv/).
- **Postgres** (the tests use a real database; `tests/conftest.py` hands each test a throwaway
  schema in a `rung_test` database).

```bash
uv sync                                   # install the core + dev dependencies

# a local Postgres for the tests (any Postgres works; this is one quick way):
docker run -d --name rung-pg -p 5432:5432 \
  -e POSTGRES_USER=rung -e POSTGRES_PASSWORD=rung -e POSTGRES_DB=rung \
  postgres:17-alpine
psql postgresql://rung:rung@localhost:5432/rung \
  -c 'CREATE DATABASE rung_test;'
```

The test database URL defaults to `postgresql://rung:rung@localhost:5432/rung_test`;
override it with the `DATABASE_URL_TEST` environment variable.

## The checks (mirrored by CI)

Every PR runs lint → type-check → tests. Run them locally before pushing:

```bash
uv tool run ruff check rung/ tests/    # lint
uv tool run ty check rung/             # type-check
uv run pytest                                        # tests + coverage floor
```

A few contracts are **AST-enforced** by the test suite, so they fail loudly rather than at runtime:

- **All HTTP goes through `http.make_session()`** (`tests/test_http.py`) — the single, honest session
  chokepoint. The published default sends an honest User-Agent and does **not** spoof a browser
  fingerprint; please keep it that way (impersonation stays opt-in/off-by-default).
- **Import layering** (`tests/test_import_layering.py`) — the base layer stays dependency-light, the
  import graph stays acyclic, and nothing imports the CLI.

Coding style follows modern, typed Python (PEP 604 unions, `pathlib`, explicit conditions); match the
surrounding code. EAFP `try/except` is expected at the scraper's external-data boundaries (parsing
untrusted JSON/HTML); CLI commands use `print()` for user output, not logging.

## Extending the framework: write a plugin

The core ships its heavier stages **unplugged** — `scrape-company-stores`, `scrape-menus`,
`compare-stores`, `recon`, and friends resolve to registry **stubs** that raise `StageNotAvailable`
until something provides them. A *plugin* provides them. No core changes required.

### The seam

`rung/registry.py` is a tiny string-keyed registry:

- `registry.register(name, impl)` — provide the implementation for a stage.
- `registry.resolve(name)` — the CLI looks a stage up here (returns the stub if unplugged).
- `registry.load_plugins()` — called once at CLI startup; discovers every plugin via the
  `rung.plugins` entry-point group and runs its registrar.

### A minimal plugin

[`examples/example_plugin.py`](examples/example_plugin.py) is a complete, runnable example (it
returns obviously-fake sample data). The shape:

```python
from rung import registry

def register() -> None:
    registry.register("compare.run", my_compare_run)
    registry.register("compare.print", my_compare_print)

def my_compare_run(conn, state: str) -> dict:
    ...   # your real comparison; returns whatever your print impl expects

def my_compare_print(report) -> None:
    ...   # render it the way `compare-stores` would
```

Ship that in your own package and point the entry point at your registrar in *your* `pyproject.toml`:

```toml
[project.entry-points."rung.plugins"]
my-overlay = "my_package.plugin:register"
```

Install your package alongside `rung`, and the CLI picks it up automatically —
`compare-stores` now runs your code instead of the stub.

### The stage contract

The stage **names** (and the arguments the CLI passes them) are the contract between the CLI and a
plugin. The stages below are the ones the built-in CLI verbs resolve — i.e. the **reference
application's** pipeline (roster → each entity's own site → reconcile → snapshot each catalog). A
plugin *replaces* these to run the reference pipeline against your own targets; to build a
**different** domain shape (your own records, schema, and stages), use the engine directly as a
library — see [`docs/build-your-own-domain.md`](docs/build-your-own-domain.md). The resolvable
stages are:

| Stage name | Backs the CLI command |
|---|---|
| `company_stores.run` / `company_stores.print` | `scrape-company-stores` |
| `menus.run` / `menus.print` | `scrape-menus` |
| `compare.run` / `compare.print` | `compare-stores` |
| `recon.run` | `recon` |
| `bootstrap.dutchie` | `bootstrap-dutchie` |
| `bootstrap.pools` | `bootstrap-pools` |
| `analyze` | `analyze` |

The exact arguments each stage receives are the `_stage("…")` call sites in
[`rung/cli.py`](rung/cli.py) — that's the authoritative signature list. A
plugin only needs to provide the stages it cares about; anything it leaves unplugged stays a stub.

### Testing a plugin

You can register directly (no entry point needed) in a test, exactly as
[`tests/test_example_plugin.py`](tests/test_example_plugin.py) does:

```python
from rung import registry
from my_package import plugin

plugin.register()
assert registry.resolve("compare.run") is not plugin  # it's your impl, not the stub
```

## Submitting changes

1. Fork, branch, and make your change with tests.
2. Make sure the three checks above pass.
3. Open a PR describing the change. CI runs the same checks on a Postgres service container.

Be respectful of the sites you scrape: honor `robots.txt` and terms of service, and don't add
evasion or rate-limit-circumvention to the public core.
