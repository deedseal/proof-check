"""Allowlist grammar, fail-closed refusals, path handling, and the rules/v0 vector corpus."""

from __future__ import annotations

import pytest

from conftest import CORPUS, VECTORS, materialize, vector_params
from proof_check.contracts import RESERVED_REASON_CODES, DetailCode, ReasonCode, Verdict
from proof_check.scope import (
    UNIVERSAL_PROBE,
    PathRefusal,
    PathRefusalCode,
    RefusalCode,
    ScopeRefusal,
    changed_path_set,
    compile_scope,
    matches,
    outside_paths,
    parse_manifest,
    parse_pr_body_allowlist,
    refuse_entry,
)
from proof_check.verify import ConfigurationError, evaluate

# --------------------------------------------------------------------------- PR body parsing


def test_parse_pr_body_takes_bullets_with_optional_backticks_and_ignores_prose():
    body = "intro\n\n## Allowlist\n\n- `MAP.md`\n* tools/acceptance/*.py\n- docs/fixtures/**\nprose line\n\n## Next\n- not/an/entry\n"
    assert parse_pr_body_allowlist(body) == ("MAP.md", "tools/acceptance/*.py", "docs/fixtures/**")


@pytest.mark.parametrize(
    "body, code",
    [
        ("no section here\n", RefusalCode.NO_SECTION),
        ("## Allowlist\n\n(prose only)\n", RefusalCode.EMPTY_SECTION),
        ("## Allowlist\n\n- ``\n", RefusalCode.EMPTY_SECTION),
        ("## Allowlist\n\n- a\n\n## Allowlist\n\n- b\n", RefusalCode.DUPLICATE_SECTION),
        ("##   Allowlist  \n- a\n## Allowlist\n- b\n", RefusalCode.DUPLICATE_SECTION),
    ],
)
def test_parse_pr_body_fails_closed(body, code):
    with pytest.raises(ScopeRefusal) as info:
        parse_pr_body_allowlist(body)
    assert info.value.code is code


def test_parse_manifest_ignores_blank_and_comment_lines_and_refuses_empty():
    assert parse_manifest("# scope\n\nREADME.md\n  docs/** \n") == ("README.md", "docs/**")
    with pytest.raises(ScopeRefusal) as info:
        parse_manifest("# only comments\n\n")
    assert info.value.code is RefusalCode.EMPTY_SECTION


# --------------------------------------------------------------------------- entry grammar


@pytest.mark.parametrize(
    "entry, code",
    [
        ("*", RefusalCode.BARE_WILDCARD),
        ("**", RefusalCode.BARE_WILDCARD),
        ("/README.md", RefusalCode.LEADING_SLASH),
        ("/**", RefusalCode.LEADING_SLASH),
        ("docs/../README.md", RefusalCode.PARENT_TRAVERSAL),
        ("..", RefusalCode.PARENT_TRAVERSAL),
        ("../x", RefusalCode.PARENT_TRAVERSAL),
        ("x/..", RefusalCode.PARENT_TRAVERSAL),
        (" /**", RefusalCode.EMPTY_PREFIX),
        ("????*", RefusalCode.UNIVERSAL_SWALLOW),
        ("**/*", RefusalCode.UNIVERSAL_SWALLOW),
        ("[!\\]*", RefusalCode.UNIVERSAL_SWALLOW),
        ("*.bin", RefusalCode.UNIVERSAL_SWALLOW),
        ("", RefusalCode.EMPTY_ENTRY),
        ("   ", RefusalCode.EMPTY_ENTRY),
        ("a\x00b", RefusalCode.CONTROL_CHARACTER),
        ("a\nb", RefusalCode.CONTROL_CHARACTER),
        ("a\udcffb", RefusalCode.INVALID_ENCODING),
    ],
)
def test_refused_entries(entry, code):
    with pytest.raises(ScopeRefusal) as info:
        refuse_entry(entry)
    assert info.value.code is code


@pytest.mark.parametrize("entry", ["README.md", "docs/**", "src/*.py", "tests/test_?.py", "a..b", "..hidden/x", "docs/img/[a-z].png"])
def test_accepted_entries(entry):
    refuse_entry(entry)
    scope = compile_scope([entry])
    assert not scope.covers(UNIVERSAL_PROBE)


def test_wildcards_inside_a_prefix_are_literal_and_cover_nothing_real():
    """Prior-art semantics: '<prefix>/**' is a plain string prefix, so '*/**' only covers paths starting with '*/'."""
    scope = compile_scope(["*/**"])
    assert not scope.covers(UNIVERSAL_PROBE)
    assert not scope.covers("src/x.py")
    assert scope.covers("*/x.py")


def test_compile_scope_classifies_kinds_and_refuses_empty():
    scope = compile_scope(["README.md", "docs/**", "src/*.py"])
    assert [e.kind for e in scope.entries] == ["exact", "prefix", "glob"]
    with pytest.raises(ScopeRefusal):
        compile_scope([])
    with pytest.raises(ScopeRefusal):
        compile_scope(["README.md", "*"])


# --------------------------------------------------------------------------- matching


@pytest.mark.parametrize(
    "path, entry, expected",
    [
        ("README.md", "README.md", True),
        ("readme.md", "README.md", False),
        ("docs/a/deep/file.png", "docs/**", True),
        ("docs", "docs/**", False),
        ("docs-site/x", "docs/**", False),
        ("src/x.ts", "docs/**", False),
        ("docs/a.md", "docs/*.md", True),
        ("docs/sub/a.md", "docs/*.md", True),  # fnmatch wildcards cross '/'
        ("docs/a.txt", "docs/*.md", False),
        ("tests/test_mod.py", "tests/test_*.py", True),
        ("tests/mod_test.py", "tests/test_*.py", False),
    ],
)
def test_matches(path, entry, expected):
    assert matches(path, entry) is expected


def test_outside_paths_is_sorted_and_deduplicated():
    scope = compile_scope(["docs/**"])
    assert outside_paths(scope, ["z.txt", "a.txt", "docs/x", "a.txt"]) == ("a.txt", "z.txt")


# --------------------------------------------------------------------------- changed paths


def test_changed_path_set_includes_both_sides_of_a_rename_sorted():
    assert changed_path_set([("docs/new.md", "src/old.md"), ("README.md", None)]) == ("README.md", "docs/new.md", "src/old.md")


@pytest.mark.parametrize(
    "records, code",
    [
        ([("", None)], PathRefusalCode.EMPTY),
        ([("/etc/passwd", None)], PathRefusalCode.ABSOLUTE),
        ([("docs/../x", None)], PathRefusalCode.PARENT_TRAVERSAL),
        ([("a\x00b", None)], PathRefusalCode.CONTROL_CHARACTER),
        ([("a\udcffb", None)], PathRefusalCode.INVALID_ENCODING),
        ([("docs//a", None)], PathRefusalCode.NON_NORMAL),
        ([("./a", None)], PathRefusalCode.NON_NORMAL),
        ([("a/", None)], PathRefusalCode.NON_NORMAL),
        ([("ok.md", "docs/./x")], PathRefusalCode.NON_NORMAL),
        ([("café.md", None), ("café.md", None)], PathRefusalCode.NORMALIZATION_COLLISION),
    ],
)
def test_changed_path_set_refuses(records, code):
    with pytest.raises(PathRefusal) as info:
        changed_path_set(records)
    assert info.value.code is code


# --------------------------------------------------------------------------- vector corpus


def test_corpus_covers_every_vector_family_in_the_fixture_plan():
    families = {v["family"] for v in VECTORS}
    assert families == {"scope", "stale", "mixed_head", "producer_reviewer", "missing_ambiguous"}
    assert len({v["id"] for v in VECTORS}) == len(VECTORS)


@pytest.mark.parametrize("vector", vector_params())
def test_vector(vector):
    policy, evidence = materialize(CORPUS, vector)
    expected = vector["expected"]
    if expected.get("configuration_error"):
        with pytest.raises(ConfigurationError):
            evaluate(policy, evidence)
        return
    result = evaluate(policy, evidence)
    assert result.verdict.value == expected["verdict"], result.explanation
    assert sorted(c.value for c in result.reason_codes) == sorted(expected["reason_codes"]), result.explanation
    assert sorted(c.value for c in result.detail_codes) == sorted(expected["detail_codes"]), result.explanation
    if "outside_paths" in expected:
        assert list(result.outside_paths) == expected["outside_paths"]
    else:
        assert result.outside_paths is None
    if "context_contains" in expected:
        assert any(expected["context_contains"] in note for note in result.context), result.context
    assert result.explanation


def test_acceptance_bar_zero_incorrect_pass():
    incorrect = []
    for vector in VECTORS:
        if vector["expected"].get("configuration_error"):
            continue
        policy, evidence = materialize(CORPUS, vector)
        result = evaluate(policy, evidence)
        if result.verdict is Verdict.PASS and vector["expected"]["verdict"] != "PASS":
            incorrect.append(vector["id"])
    assert incorrect == []


def test_rules_v0_invariants_hold_across_the_corpus():
    verdicts = set()
    for vector in VECTORS:
        if vector["expected"].get("configuration_error"):
            continue
        policy, evidence = materialize(CORPUS, vector)
        result = evaluate(policy, evidence)
        verdicts.add(result.verdict)
        assert not (set(result.reason_codes) & RESERVED_REASON_CODES), vector["id"]
        if result.verdict is Verdict.PASS:
            assert result.reason_codes == (), vector["id"]
            assert result.outside_paths is None
            assert DetailCode.ALL_CHANGED_PATHS_WITHIN_DECLARED_SCOPE in result.detail_codes
        elif result.verdict is Verdict.FAIL:
            assert result.reason_codes == (ReasonCode.SCOPE_ESCAPE,), vector["id"]
            assert result.detail_codes == (DetailCode.PATH_OUTSIDE_DECLARED_SCOPE,)
            assert result.outside_paths and list(result.outside_paths) == sorted(set(result.outside_paths))
        else:
            assert result.reason_codes, vector["id"]
            assert ReasonCode.SCOPE_ESCAPE not in result.reason_codes
            assert result.outside_paths is None
    assert verdicts == {Verdict.PASS, Verdict.FAIL, Verdict.INDETERMINATE}


def test_evaluation_is_deterministic():
    for vector in VECTORS[:8]:
        if vector["expected"].get("configuration_error"):
            continue
        policy, evidence = materialize(CORPUS, vector)
        assert evaluate(policy, evidence) == evaluate(policy, evidence)
