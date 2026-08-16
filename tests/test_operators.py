"""The cross-state operator key's contract.

`companies` is UNIQUE (canonical_name, state), so before this crosswalk the MSO test asked a
per-state table a cross-state question by string identity — which fails in both directions.
`rung/operators.py` fixes both; these pin that neither fix quietly reintroduces the other.
See `reports/adversarial_log.md` Round 24.
"""


from rung import operators

_EVIDENCE_FIELDS = ("source_type", "source_ref", "source_url", "confidence", "retrieved_at")
_CONFIDENCE_GRADES = frozenset({"verified", "reported", "inferred"})


def test_a_mapped_banner_folds_to_its_parent() -> None:
    assert operators.parent_of("Zen Leaf") == "Verano Holdings"
    assert operators.parent_of("MÜV") == "Verano Holdings"
    assert operators.parent_of("Beyond Hello") == "Jushi Holdings"


def test_the_fold_is_case_and_punctuation_insensitive() -> None:
    """The roster publishes `CURALEAF`, `Curaleaf` and `Zen Leaf`/`ZenLeaf` interchangeably."""
    assert operators.parent_of("curaleaf") == operators.parent_of("CURALEAF") == "Curaleaf Holdings"
    assert operators.parent_of("ZenLeaf") == operators.parent_of("Zen Leaf")


def test_an_unmapped_banner_is_its_own_operator() -> None:
    """An operator nobody has mapped is presumed independent, not dropped."""
    assert operators.parent_of("Twin Cities Greens") == "Twin Cities Greens"


def test_a_generic_storefront_name_has_no_key() -> None:
    """The false-merge fix. `None` means UNRESOLVABLE, not `independent`.

    Counting `The Dispensary` as one operator across three states invented an MSO; counting it as an
    independent asserts something we equally do not know. It has to leave the numerator and stay in
    the denominator, which is what a null key buys.
    """
    assert operators.parent_of("The Dispensary") is None
    assert operators.operator_key("The Dispensary") is None
    assert not operators.is_corporate_identity("the dispensary")
    assert operators.reason_not_an_identity("The Dispensary")


def test_the_default_key_folds_only_the_ATTESTED_relationships() -> None:
    """The conservative default, and it is conservative for a measured reason.

    Folding unmapped banners by their normalized form looks like free cleanup — it would unite
    `Good Day Farm` (MO) with `Gooddayfarm` (LA, AR). On the live table it also merges case and
    spacing variants of generic shop names across states: 72 multi-state operators become 107, and
    only six of the thirty-five new ones are corporate parents. The rest (`bestbuds`, `bloom`,
    `elevate`, `budz`, `cream`, `elite`) are unrelated businesses fused into invented MSOs, and the
    coded regime test moves p = 0.079 → 0.0017 on the strength of it. So it is available behind a
    flag and is never the default.
    """
    assert operators.operator_key("Good Day Farm") == "Good Day Farm"
    assert operators.operator_key("Gooddayfarm") == "Gooddayfarm"
    assert (operators.operator_key("Good Day Farm", fold_spelling=True)
            == operators.operator_key("Gooddayfarm", fold_spelling=True))


def test_the_key_of_a_mapped_banner_is_the_parent_label() -> None:
    assert operators.operator_key("Zen Leaf") == "Verano Holdings"
    assert operators.operator_key("MUV") == "Verano Holdings"


def test_lookup_is_exact_not_substring() -> None:
    """A key, not a matcher. Substring matching is how `The Dispensary` swallows its neighbours."""
    assert operators.parent_of("The Dispensary of Utah") == "The Dispensary of Utah"
    assert operators.parent_of("Zen Leaf Dayton") == "Zen Leaf Dayton"


def test_every_parent_carries_its_evidence() -> None:
    doc = operators.parents_doc()["parents"]
    for parent, spec in doc.items():
        missing = [f for f in _EVIDENCE_FIELDS if not str(spec.get(f, "")).strip()]
        assert not missing, f"{parent} missing {missing}"
        assert spec["confidence"] in _CONFIDENCE_GRADES, f"{parent}: {spec['confidence']}"
        assert spec.get("banners"), f"{parent} folds no banners"


def test_the_unattested_fold_is_still_graded_inferred() -> None:
    """Round 24 named Cresco->Sunnyside as the one unattested link. A grade is not a formality:
    if it silently became `verified` without a filing being read, a result resting on this single
    row would stop announcing that it does."""
    assert operators.parent_record("Cresco Labs")["confidence"] == "inferred"


def test_no_banner_is_claimed_by_two_parents() -> None:
    seen: dict[str, str] = {}
    for parent, spec in operators.parents_doc()["parents"].items():
        for banner in spec["banners"]:
            key = operators.normalize_banner(str(banner))
            assert key not in seen, f"{banner!r} claimed by both {seen[key]} and {parent}"
            seen[key] = parent


def test_a_per_banner_source_override_names_a_real_banner() -> None:
    """`banner_sources` exists so a fold backed by a DIFFERENT document carries that document.

    Its failure mode is a stale key: rename the banner, leave the override behind, and the banner
    silently reverts to the row's default filing — a citation that does not say what it is cited for,
    which is the defect Round 24 caught twice in D4's reference list.
    """
    for parent, spec in operators.parents_doc()["parents"].items():
        banners = {str(b) for b in spec["banners"]}
        for banner, override in (spec.get("banner_sources") or {}).items():
            assert banner in banners, f"{parent}: banner_sources key {banner!r} is not one of its banners"
            missing = [f for f in _EVIDENCE_FIELDS if not str(override.get(f, "")).strip()]
            assert not missing, f"{parent}/{banner} override missing {missing}"


def test_a_generic_name_is_never_also_a_mapped_banner() -> None:
    """The two halves must not contradict: a name cannot be both unresolvable and folded."""
    generic = {operators.normalize_banner(n)
               for n in operators.parents_doc()["not_corporate_identities"]}
    mapped = {operators.normalize_banner(str(b))
              for spec in operators.parents_doc()["parents"].values() for b in spec["banners"]}
    assert not generic & mapped, f"listed as both generic and mapped: {sorted(generic & mapped)}"


def test_null_and_empty_names_are_handled() -> None:
    assert operators.parent_of(None) is None
    assert operators.parent_of("") is None
    assert operators.operator_key(None) is None
