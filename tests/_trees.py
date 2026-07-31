"""Which source trees exist HERE — so a guard can scan the private tree and still ship.

Several tests sweep the whole codebase for a contract: "a policy helper leaves the commit to its
caller", "every roster replace restores the geocode cache". Those are worth having, and the public
`rung` repo should have them for the code it ships.

The problem is arithmetic. Each of those guards carries an ANTI-VACUITY FLOOR — "expected at least 3
roster-replacing functions; found 1" — because a sweep that matches nothing satisfies every
assertion perfectly, and this project has been bitten by that repeatedly. But the floor is counted
over the FULL repo, and the public build ships `rung/` alone: no `rung_intel/`, no `scripts/`. So the
guard shipped, scanned one tree, counted one, and failed on a number a public contributor could
neither reach nor understand.

Both halves matter, and dropping either is wrong:

  * Delete the floor and the guard becomes vacuous in the public repo — worse than absent, because it
    reads as coverage.
  * Ship the private floor and the guard fails for everyone who clones.

So a floor is declared PER TREE and summed over the trees that are actually present. In this repo
that is the full number; in the public repo it is `rung/`'s share; and in both, a sweep that finds
less than its trees promise still fails.

No DB, no network. Import-safe.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The three first-party source trees, in the order a sweep should walk them.
PUBLIC_CORE = REPO / "rung"
OVERLAY = REPO / "rung_intel"
ANALYSIS = REPO / "scripts"


def present(*trees: Path) -> list[Path]:
    """The given trees that exist. The public build ships only `rung/`."""
    return [t for t in trees if t.is_dir()]


def floor(**per_tree: int) -> int:
    """Sum the declared minimums over the trees that are actually here.

    Call it with the tree NAMES as keywords, so the declaration reads as the census it is::

        floor(rung=1, rung_intel=1, scripts=1)   # 3 in the monorepo, 1 in the public repo

    A tree absent from the keywords contributes nothing, which is how a guard says "I do not expect
    to find any of these there".
    """
    return sum(n for name, n in per_tree.items() if (REPO / name).is_dir())


def is_public_build() -> bool:
    """Is this the assembled public tree rather than the monorepo?

    Asked as "is the overlay missing" rather than by looking for a marker file, because the overlay's
    absence is the actual thing every caller cares about — a marker could be added to a tree that
    still had it.
    """
    return not OVERLAY.is_dir()
