"""Receipt production, schema closure, digests, offline verification, and tampered twins."""

from __future__ import annotations

import json

import pytest

from conftest import CORPUS, SOURCE_COMMIT, VECTORS, materialize, vector_params
from proof_check.canonical import canonical_bytes, canonical_sha256
from proof_check.contracts import RESERVED_REASON_CODES, Receipt, Verdict, validate_receipt
from proof_check.verify import build_receipt, evaluate, receipt_digest, verify_receipt


def _receipts():
    for vector in VECTORS:
        if vector["expected"].get("configuration_error"):
            continue
        policy, evidence = materialize(CORPUS, vector)
        evaluation = evaluate(policy, evidence)
        yield vector, policy, evaluation, build_receipt(evaluation, evidence, SOURCE_COMMIT)


RECEIPTS = list(_receipts())


def _params():
    return [pytest.param(item, id=item[0]["id"]) for item in RECEIPTS]


@pytest.mark.parametrize("item", _params())
def test_every_receipt_validates_against_the_schema(item):
    vector, _policy, evaluation, receipt = item
    data = receipt.to_dict()
    validate_receipt(data)
    assert data["verdict"] == vector["expected"]["verdict"]
    assert data["tool"] == {"name": "proof-check", "version": "0.0.0", "source_commit": SOURCE_COMMIT, "rules_version": "rules/v0"}
    assert data["limitations"] and data["does_not_prove"]
    assert ("outside_paths" in data) == (data["verdict"] == "FAIL")
    assert not (set(data["reason_codes"]) & {c.value for c in RESERVED_REASON_CODES})
    assert Receipt.from_dict(data) == receipt


@pytest.mark.parametrize("item", _params())
def test_every_receipt_verifies_offline_with_and_without_its_policy(item):
    _vector, policy, _evaluation, receipt = item
    data = receipt.to_dict()
    assert data["receipt_digest"] == receipt_digest(data)
    result = verify_receipt(canonical_bytes(data))
    assert result.ok, result.findings
    pretty = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    assert verify_receipt(pretty).ok
    if policy.get("schema_version") == "proof-check-policy/v1":
        assert verify_receipt(data, policy=policy).ok
        assert data["policy"]["policy_digest"] == canonical_sha256(policy)
        wrong = {**policy, "receipt": {"mode": "off"}}
        assert not verify_receipt(data, policy=wrong).ok


@pytest.mark.parametrize("item", _params())
def test_byte_tampered_twins_fail_verification(item):
    """Flip bytes across the canonical receipt; every twin must fail to verify."""
    _vector, _policy, _evaluation, receipt = item
    original = canonical_bytes(receipt.to_dict())
    assert verify_receipt(original).ok
    positions = set(range(0, len(original), 13))
    digest_at = original.rfind(b'"receipt_digest":"') + len(b'"receipt_digest":"')
    positions.update(range(digest_at, digest_at + 64, 7))
    verdict_at = original.find(b'"verdict":"') + len(b'"verdict":"')
    positions.update(range(verdict_at, verdict_at + 4))
    positions.update({0, len(original) - 1})
    survivors = []
    for position in sorted(positions):
        twin = bytearray(original)
        twin[position] ^= 0x01 if twin[position] not in (0x30, 0x31) else 0x02
        if bytes(twin) == original:
            continue
        if verify_receipt(bytes(twin)).ok:
            survivors.append(position)
    assert survivors == [], survivors


def test_semantic_tampering_is_caught():
    fail = next(item for item in RECEIPTS if item[3].verdict is Verdict.FAIL)
    data = fail[3].to_dict()

    def resign(doc):
        doc = json.loads(json.dumps(doc))
        doc["receipt_digest"] = receipt_digest(doc)
        return doc

    flipped = resign({**data, "verdict": "PASS", "reason_codes": [], "detail_codes": ["ALL_CHANGED_PATHS_WITHIN_DECLARED_SCOPE"]})
    del flipped["outside_paths"]
    flipped["receipt_digest"] = receipt_digest(flipped)
    result = verify_receipt(flipped)
    assert not result.ok and any("PASS" in f for f in result.findings)

    trimmed = resign({**data, "outside_paths": data["outside_paths"][:1]})
    result = verify_receipt(trimmed)
    assert not result.ok and any("outside_paths" in f for f in result.findings)

    widened = resign({**data, "policy": {**data["policy"], "allowed_entries": ["README.md", "**/*"]}})
    result = verify_receipt(widened)
    assert not result.ok

    reserved = resign({**data, "verdict": "INDETERMINATE", "reason_codes": ["MIXED_HEAD_EVIDENCE"], "detail_codes": []})
    del reserved["outside_paths"]
    reserved["receipt_digest"] = receipt_digest(reserved)
    result = verify_receipt(reserved)
    assert not result.ok and any("reserved" in f for f in result.findings)

    incomplete = next(item for item in RECEIPTS if item[0]["id"] == "missing-changed-set-count-mismatch")[3].to_dict()
    laundered = resign({**incomplete, "verdict": "PASS", "reason_codes": [], "detail_codes": ["ALL_CHANGED_PATHS_WITHIN_DECLARED_SCOPE"]})
    result = verify_receipt(laundered)
    assert not result.ok and any("INDETERMINATE" in f for f in result.findings)


def test_malformed_input_fails_closed():
    assert not verify_receipt(b"").ok
    assert not verify_receipt(b"[]").ok
    assert not verify_receipt(b'{"a":1,"a":2}').ok
    assert not verify_receipt(b"\xff\xfe").ok
    assert not verify_receipt({"schema_version": "proof-check-receipt/v1"}).ok


def test_receipts_are_deterministic_and_carry_stable_ids():
    vector = next(v for v in VECTORS if v["id"] == "scope-fail-readme-only-23-paths")
    policy, evidence = materialize(CORPUS, vector)
    one = build_receipt(evaluate(policy, evidence), evidence, SOURCE_COMMIT)
    two = build_receipt(evaluate(policy, evidence), evidence, SOURCE_COMMIT)
    assert canonical_bytes(one.to_dict()) == canonical_bytes(two.to_dict())
    assert one.receipt_id.startswith("pcr1-")
    ids = {item[3].receipt_id for item in RECEIPTS}
    identities = {(item[3].subject.repository, item[3].subject.pr_number, item[3].subject.evaluated_sha, item[3].observation.watermark) for item in RECEIPTS}
    assert len(ids) == len(identities)


def test_fail_receipt_names_the_outside_paths_sorted_and_the_pass_twin_carries_no_reasons():
    by_id = {item[0]["id"]: item[3] for item in RECEIPTS}
    fail = by_id["scope-fail-readme-only-23-paths"].to_dict()
    assert fail["reason_codes"] == ["SCOPE_ESCAPE"]
    assert fail["detail_codes"] == ["PATH_OUTSIDE_DECLARED_SCOPE"]
    assert len(fail["outside_paths"]) == 22 and fail["outside_paths"] == sorted(fail["outside_paths"])
    assert fail["policy"]["source_type"] == "author_pr_body" and fail["policy"]["trust"] == "advisory"
    twin = by_id["scope-pass-readme-only-twin"].to_dict()
    assert twin["reason_codes"] == [] and "outside_paths" not in twin
    merge_group = by_id["mixed-head-merge-group-context-only"].to_dict()
    assert merge_group["detail_codes"] == ["ALL_CHANGED_PATHS_WITHIN_DECLARED_SCOPE", "MERGE_GROUP_COORDINATES_COHERENT"]
    assert [c["id"] for c in merge_group["observations"]["checks"]] == [3, 7]
    assert any("distinct commit SHAs" in line for line in merge_group["limitations"])


def test_invalid_unicode_path_is_recorded_escaped_and_still_verifies():
    receipt = {item[0]["id"]: item[3] for item in RECEIPTS}["ambiguous-path-invalid-unicode"].to_dict()
    filenames = [r["filename"] for r in receipt["observations"]["changed_paths"]["records"]]
    assert "docs/a\\udcffb.txt" in filenames
    assert any("ASCII-escaped" in line for line in receipt["limitations"])
    assert verify_receipt(canonical_bytes(receipt)).ok
