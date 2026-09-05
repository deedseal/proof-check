"""Schemas, canonicalization, typed models, and the public-vocabulary acceptance grep."""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from conftest import CORPUS, REPO_ROOT
from proof_check import canonical, contracts
from proof_check.canonical import CanonicalizationError, canonical_bytes, canonical_sha256
from proof_check.contracts import (
    POLICY_SCHEMA_FILE,
    RECEIPT_SCHEMA_FILE,
    RESERVED_REASON_CODES,
    ContractError,
    DetailCode,
    PolicyDocument,
    ReasonCode,
    SchemaViolation,
    Verdict,
    load_schema,
    validate_policy,
    validate_receipt,
)

# --------------------------------------------------------------------------- schemas


def test_schemas_load_and_are_valid_draft_2020_12():
    receipt = load_schema(RECEIPT_SCHEMA_FILE)
    policy = load_schema(POLICY_SCHEMA_FILE)
    assert receipt["title"] == "proof-check-receipt/v1"
    assert policy["title"] == "proof-check-policy/v1"
    assert receipt["properties"]["schema_version"]["const"] == contracts.RECEIPT_SCHEMA_VERSION
    assert policy["properties"]["schema_version"]["const"] == contracts.POLICY_SCHEMA_VERSION


def test_enumerations_match_schema_vocabularies_exactly():
    receipt = load_schema(RECEIPT_SCHEMA_FILE)
    assert [c.value for c in ReasonCode] == receipt["$defs"]["reasonCode"]["enum"]
    assert [c.value for c in DetailCode] == receipt["$defs"]["detailCode"]["enum"]
    assert [v.value for v in Verdict] == receipt["properties"]["verdict"]["enum"]
    assert RESERVED_REASON_CODES == {ReasonCode.MIXED_HEAD_EVIDENCE, ReasonCode.PRODUCER_REVIEWER_CONFLICT}


def test_outside_paths_must_be_non_empty_and_deduplicated():
    schema = load_schema(RECEIPT_SCHEMA_FILE)["properties"]["outside_paths"]
    assert schema["minItems"] == 1
    assert schema["uniqueItems"] is True
    assert schema["description"].endswith("; never empty.")


def test_policy_schema_entry_grammar_refuses_what_the_description_says():
    pattern = re.compile(load_schema(POLICY_SCHEMA_FILE)["$defs"]["scopeEntry"]["pattern"])
    for refused in ("*", "**", "/README.md", "docs/../x", "..", "../x", "x/.."):
        assert pattern.match(refused) is None, refused
    for accepted in ("README.md", "docs/**", "src/*.py", "a..b", "..hidden"):
        assert pattern.match(accepted) is not None, accepted


def test_validate_policy_rejects_unknown_members_and_wrong_constants():
    policy = CORPUS["defaults"]["policy"]
    validate_policy(policy)
    with pytest.raises(SchemaViolation) as info:
        validate_policy({**policy, "extra": 1})
    assert any("extra" in message for _, message in info.value.findings)
    with pytest.raises(SchemaViolation):
        validate_policy({**policy, "verdict_policy": {"fail_exit": 0, "indeterminate_exit": 20}})
    with pytest.raises(SchemaViolation):
        validate_policy({**policy, "scope": {"source": "pr_body_allowlist", "trust": "advisory", "entries": ["*"]}})


def test_validate_receipt_reports_every_violation_with_a_pointer():
    with pytest.raises(SchemaViolation) as info:
        validate_receipt({"schema_version": "proof-check-receipt/v1"})
    pointers = {pointer for pointer, _ in info.value.findings}
    assert "/" in pointers  # missing required members are reported at the root
    assert len(info.value.findings) >= 1


# --------------------------------------------------------------------------- typed models


def test_policy_document_round_trips_and_omits_absent_entries():
    data = CORPUS["defaults"]["policy"]
    doc = PolicyDocument.from_dict(data)
    assert doc.scope.source_kind() == "pr_body_allowlist"
    assert doc.to_dict() == data
    pinned = {**data, "scope": {"source": "pinned:" + "a" * 64, "trust": "blocking", "entries": ["README.md"]}}
    doc = PolicyDocument.from_dict(pinned)
    assert doc.scope.source_kind() == "pinned"
    assert doc.scope.source_argument() == "a" * 64
    assert doc.to_dict() == pinned


def test_model_builder_rejects_wrong_shapes():
    data = CORPUS["defaults"]["policy"]
    with pytest.raises(ContractError):
        PolicyDocument.from_dict({**data, "schema_version": "proof-check-policy/v9"})
    with pytest.raises(ContractError):
        contracts.Subject.from_dict({**CORPUS["defaults"]["evidence"]["subject"], "pr_number": "42"})
    with pytest.raises(ContractError):
        contracts.Subject.from_dict({**CORPUS["defaults"]["evidence"]["subject"], "target_kind": "branch"})
    with pytest.raises(ContractError):
        contracts.Observation.from_dict({"cutoff_utc": "x", "captured_at_utc": "y"})


# --------------------------------------------------------------------------- canonical JSON


def test_rfc_8785_reference_sample_matches_byte_for_byte():
    source = (
        '{"numbers":[333333333.33333329,1E30,4.50,2e-3,0.000000000000000000000000001],'
        '"string":"\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/","literals":[null,true,false]}'
    )
    expected = (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )
    assert canonical_bytes(json.loads(source)) == expected.encode("utf-8")


@pytest.mark.parametrize(
    "value, rendered",
    [
        (0.0, "0"), (-0.0, "0"), (1.0, "1"), (1e21, "1e+21"), (1e20, "100000000000000000000"),
        (0.000001, "0.000001"), (0.0000001, "1e-7"), (123.456, "123.456"), (-1.5e-9, "-1.5e-9"),
        (9007199254740992.0, "9007199254740992"), (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"),
    ],
)
def test_number_rendering_follows_ecmascript(value, rendered):
    assert canonical_bytes(value).decode() == rendered


def test_keys_sort_by_code_point_without_whitespace_or_trailing_newline():
    data = {"b": 1, "a": 2, "é": 3, "B": 4, "": 5}
    out = canonical_bytes(data)
    assert out == b'{"":5,"B":4,"a":2,"b":1,"\xc3\xa9":3}'
    assert not out.endswith(b"\n")


def test_string_escaping_follows_rfc_8785():
    assert canonical_bytes("\x00\x1f\x7f /\\\"") == b'"\\u0000\\u001f\x7f\xe2\x80\xa8/\\\\\\""'


def test_canonicalization_refuses_what_it_cannot_round_trip():
    with pytest.raises(CanonicalizationError):
        canonical_bytes("lone \udcff surrogate")
    with pytest.raises(CanonicalizationError):
        canonical_bytes(2**53 + 1)
    with pytest.raises(CanonicalizationError):
        canonical_bytes(float("nan"))
    with pytest.raises(CanonicalizationError):
        canonical_bytes({1: "non-string key"})
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"x": object()})


def test_digest_names_exact_bytes():
    assert canonical.sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert canonical_sha256({"a": [1, 2]}) == canonical.sha256_hex(b'{"a":[1,2]}')
    assert canonical_sha256({"a": [1, 2]}) != canonical.sha256_hex(b'{"a":[1,2]}\n')


# --------------------------------------------------------------------------- vocabulary acceptance


def _prohibited_terms() -> list[str]:
    text = (REPO_ROOT / "docs" / "contract" / "claims-and-nonclaims.md").read_text(encoding="utf-8")
    section = text.split("## Prohibited or gated vocabulary", 1)[1].split("\n\n", 3)[2]
    terms = re.findall(r"`([^`]+)`", section)
    assert len(terms) == 16, terms
    return terms


def test_prohibited_vocabulary_is_absent_from_the_tree():
    """Word-boundary grep over every tracked file except the list's own home."""
    terms = _prohibited_terms()
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO_ROOT, check=True, capture_output=True
    ).stdout.decode().split("\0")
    files = [f for f in tracked if f and f != "docs/contract/claims-and-nonclaims.md"]
    pattern = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z0-9_])", re.I)
    hits = []
    for name in files:
        content = (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(content.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{name}:{number}: {line.strip()[:100]}")
    assert not hits, "\n".join(hits)
