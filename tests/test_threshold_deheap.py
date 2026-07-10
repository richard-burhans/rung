"""De-heaping invariants for the THC threshold-bunching analysis.

The naive McCrary θ cannot tell a marketing bar from round-number reporting: it assumes a smooth
density, so an integer spike at the cutoff is filed just ABOVE it and θ fires at every integer.
Canada's naive θ at the non-marketing 18% cut (+2.948) is indistinguishable from its θ at 20%
(+2.975), because 59.4% of its labels are whole integers.

These tests pin the two properties the correction depends on, on synthetic data where the truth is
known: de-heaping must KILL a pure heaping artifact, and must PRESERVE a genuine discontinuity.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("pandas")

# The analysis scripts sibling-import each other (`import _scope`), which resolves when they run as
# scripts because their own directory leads sys.path. Reproduce that here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import thc_threshold_bunching as tb


def _smooth(rng, n=200_000):
    """A smooth unimodal labeled-THC density over the 5–40 window, with no bar and no heaping."""
    return np.clip(rng.normal(24.0, 5.0, n), 5.01, 39.99)


def test_deheap_spreads_integers_and_leaves_decimals_alone():
    rng = np.random.default_rng(0)
    thc = np.array([20.0, 20.0, 19.4, 25.0, 21.7])
    out = tb._deheap(thc, rng)
    # Decimals are untouched; integers land strictly inside the unit they stand for.
    assert out[2] == 19.4 and out[4] == 21.7
    for value, original in ((out[0], 20.0), (out[1], 20.0), (out[3], 25.0)):
        assert original - 0.5 <= value < original + 0.5
        assert value != original
    assert len(np.unique(out[[0, 1]])) == 2  # the heap is spread, not moved as a block


def test_deheaping_kills_a_pure_heaping_artifact():
    """Round every label to the nearest integer: no bar exists, yet the naive θ sees one at 20%."""
    rng = np.random.default_rng(1)
    heaped = np.round(_smooth(rng))

    naive = tb._theta(heaped, 20.0)
    deheaped, _ = tb._deheaped_thetas(heaped, (20.0,))[20.0]

    assert naive > 1.0, "rounding alone must manufacture a large naive discontinuity"
    assert abs(deheaped) < 0.05, f"de-heaping must remove the artifact, got {deheaped:+.3f}"


def test_deheaping_preserves_a_genuine_threshold_effect():
    """Move mass from just below 20 to just above it, then round HALF the labels to integers.

    The bar is real and the data is heaped — the situation the US is actually in. θ must survive.
    """
    rng = np.random.default_rng(2)
    thc = _smooth(rng)
    just_below = (thc >= 19.0) & (thc < 20.0)
    nudged = just_below & (rng.random(len(thc)) < 0.5)   # half of them jump the bar
    thc[nudged] += 1.0

    heaped = thc.copy()
    to_round = rng.random(len(thc)) < 0.5
    heaped[to_round] = np.round(heaped[to_round])

    deheaped, sd = tb._deheaped_thetas(heaped, (20.0,))[20.0]
    placebo, _ = tb._deheaped_thetas(heaped, (24.0,))[24.0]

    assert deheaped - 2 * sd > 0, f"a real bar must clear its own noise, got {deheaped:+.3f}±{sd:.3f}"
    assert deheaped > placebo, "the real bar must exceed a non-marketing placebo cut"


def test_placebo_cuts_are_even_and_never_marketing_bars():
    # Even, because Canadian labels prefer even integers (34.9% of mass vs 24.0% odd) — an odd
    # placebo would flatter the bar by comparing it against a thinner heap.
    assert all(c % 2 == 0 for c in tb._PLACEBO_CUTS)
    assert not set(tb._PLACEBO_CUTS) & set(tb._THRESHOLDS)


def test_deheaped_thetas_is_reproducible_and_reports_undefined_cuts():
    rng = np.random.default_rng(3)
    thc = np.round(_smooth(rng))
    assert tb._deheaped_thetas(thc, (20.0,)) == tb._deheaped_thetas(thc, (20.0,))  # seeded
    # A cut below the support has no density on one side: θ is undefined, not zero.
    mean, sd = tb._deheaped_thetas(thc[thc > 18.0], (6.0,))[6.0]
    assert np.isnan(mean) and np.isnan(sd)


def test_integer_heap_index_sees_what_jitter_cannot():
    """Extra mass exactly AT the bar leaves the jittered θ unmoved but lifts the heap index."""
    base = np.concatenate([np.full(100, float(v)) for v in range(15, 30)])
    extra = np.full(200, 20.0)                       # a bar that lives at the round number
    assert tb._integer_heap_index(base, 20.0) == pytest.approx(1.0)
    assert tb._integer_heap_index(np.concatenate([base, extra]), 20.0) == pytest.approx(3.0)
