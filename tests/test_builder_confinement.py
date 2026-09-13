"""Offline proof for the N28 Builder branch-confinement ruleset payload.

No network. The validator below is the executable form of
`docs/operations/N28-BUILDER-CONFINEMENT.md` section 4: one exact assigned branch, one exact
Integration bypass actor, and the three creation/update/deletion restrictions. It rejects
configuration; it proves nothing about GitHub's runtime enforcement.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_PATH = ROOT / "examples" / "rulesets" / "n28-builder-confinement.json"
DOC_PATH = ROOT / "docs" / "operations" / "N28-BUILDER-CONFINEMENT.md"

RAW_PAYLOAD = PAYLOAD_PATH.read_text(encoding="utf-8")
DOC = DOC_PATH.read_text(encoding="utf-8")

DEFAULT_BRANCH = "main"
ASSIGNED_REF = "refs/heads/builder/n28-assignment-0001"
ACTOR_ID_PLACEHOLDER = "<BUILDER_APP_ID>"
RESOLVED_ACTOR_ID = 424242

TOP_LEVEL_KEYS = frozenset({"name", "target", "enforcement", "bypass_actors", "conditions", "rules"})
REQUIRED_RULE_TYPES = ("creation", "update", "deletion")
RESERVED_NAMES = frozenset({"main-protection", "smoke containment"})

# fnmatch metacharacters plus the bytes git-check-ref-format refuses outright.
REF_FORBIDDEN = set(" ~^:?*[\\") | {chr(code) for code in range(0x00, 0x20)} | {chr(0x7F)}


def _ref_is_exact(ref: object) -> bool:
    """True only for a single, literal, well-formed branch ref."""
    if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
        return False
    name = ref[len("refs/heads/"):]
    if not name or name.startswith("/") or name.endswith("/") or "//" in name:
        return False
    if ".." in name or "@{" in name or name.endswith(".lock") or name.endswith("."):
        return False
    if any(char in REF_FORBIDDEN for char in name):
        return False
    return not any(segment.startswith(".") or segment.endswith(".lock") for segment in name.split("/"))


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(payload: object, *, default_branch: str = DEFAULT_BRANCH, allow_placeholder: bool = False) -> set[str]:
    """Return the closed set of reason codes that refuse `payload`. Empty means admissible."""
    codes: set[str] = set()
    if not isinstance(payload, dict):
        return {"PAYLOAD_NOT_OBJECT"}

    if set(payload) - TOP_LEVEL_KEYS:
        codes.add("EXTRA_TOP_LEVEL_KEY")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        codes.add("NAME_MISSING")
    elif name in RESERVED_NAMES:
        codes.add("NAME_RESERVED")

    if payload.get("target", "branch") != "branch":
        codes.add("TARGET_NOT_BRANCH")
    if payload.get("enforcement") != "active":
        codes.add("ENFORCEMENT_NOT_ACTIVE")

    codes |= _validate_conditions(payload.get("conditions"), default_branch)
    codes |= _validate_rules(payload.get("rules"))
    codes |= _validate_bypass(payload.get("bypass_actors"), allow_placeholder)
    return codes


def _validate_conditions(conditions: object, default_branch: str) -> set[str]:
    if not isinstance(conditions, dict) or not isinstance(conditions.get("ref_name"), dict):
        return {"CONDITIONS_MALFORMED"}
    ref_name = conditions["ref_name"]
    include, exclude = ref_name.get("include"), ref_name.get("exclude", [])
    if not isinstance(include, list) or not isinstance(exclude, list):
        return {"CONDITIONS_MALFORMED"}

    codes: set[str] = set()
    if exclude:
        codes.add("EXCLUDE_NOT_EMPTY")
    if len(include) != 1:
        codes.add("INCLUDE_NOT_EXACTLY_ONE")
    for ref in include:
        if ref == "~DEFAULT_BRANCH" or ref == default_branch or ref == f"refs/heads/{default_branch}":
            codes.add("INCLUDE_DEFAULT_BRANCH")
        elif isinstance(ref, str) and ref.startswith("refs/tags/"):
            codes.add("INCLUDE_TAG_REF")
        elif not _ref_is_exact(ref):
            codes.add("INCLUDE_NOT_EXACT_REF")
    return codes


def _validate_rules(rules: object) -> set[str]:
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        return {"RULES_NOT_EXACT_SET"}
    types = [rule.get("type") for rule in rules]
    if len(types) != len(REQUIRED_RULE_TYPES) or set(types) != set(REQUIRED_RULE_TYPES):
        return {"RULES_NOT_EXACT_SET"}
    return set()


def _validate_bypass(actors: object, allow_placeholder: bool) -> set[str]:
    if not isinstance(actors, list) or len(actors) != 1 or not isinstance(actors[0], dict):
        return {"BYPASS_NOT_EXACTLY_ONE"}
    actor = actors[0]
    codes: set[str] = set()
    if actor.get("actor_type") != "Integration":
        codes.add("BYPASS_ACTOR_TYPE_NOT_INTEGRATION")
    if actor.get("bypass_mode", "always") != "always":
        codes.add("BYPASS_MODE_NOT_ALWAYS")
    actor_id = actor.get("actor_id")
    if not (_is_positive_int(actor_id) or (allow_placeholder and actor_id == ACTOR_ID_PLACEHOLDER)):
        codes.add("BYPASS_ACTOR_ID_INVALID")
    return codes


def resolved() -> dict:
    """The shipped payload with its single substitution slot filled, as the Owner would apply it."""
    payload = json.loads(RAW_PAYLOAD)
    payload["bypass_actors"][0]["actor_id"] = RESOLVED_ACTOR_ID
    return payload


def mutate(**overrides) -> dict:
    payload = resolved()
    for dotted, value in overrides.items():
        target = payload
        *path, leaf = dotted.split("__")
        for key in path:
            target = target[int(key)] if isinstance(target, list) else target[key]
        target[leaf] = value
    return payload


# --- the shipped artifact ---------------------------------------------------


def test_shipped_payload_has_the_exact_designed_shape():
    payload = json.loads(RAW_PAYLOAD)
    assert set(payload) == TOP_LEVEL_KEYS
    assert payload["name"] == "n28-builder-assignment-0001"
    assert payload["target"] == "branch"
    assert payload["enforcement"] == "active"
    assert payload["conditions"] == {"ref_name": {"include": [ASSIGNED_REF], "exclude": []}}
    assert [rule["type"] for rule in payload["rules"]] == list(REQUIRED_RULE_TYPES)
    assert payload["bypass_actors"] == [
        {"actor_id": ACTOR_ID_PLACEHOLDER, "actor_type": "Integration", "bypass_mode": "always"}
    ]


def test_shipped_payload_is_admissible_only_as_a_template():
    assert validate(json.loads(RAW_PAYLOAD), allow_placeholder=True) == set()
    assert validate(json.loads(RAW_PAYLOAD)) == {"BYPASS_ACTOR_ID_INVALID"}


def test_resolved_payload_is_admissible():
    assert validate(resolved()) == set()


def test_document_embeds_the_payload_byte_for_byte():
    blocks = re.findall(r"```json\n(.*?)```\n", DOC, re.DOTALL)
    assert blocks.count(RAW_PAYLOAD) == 1


def test_document_states_the_live_gate_and_claims_no_live_evidence():
    assert "OWNER_RULESET_ACT_REQUIRED" in DOC
    assert "Configuration bytes are not confinement evidence." in DOC
    assert "main-protection" in DOC


def test_payload_leaves_main_protection_alone():
    payload = resolved()
    assert payload["name"] not in RESERVED_NAMES
    include = payload["conditions"]["ref_name"]["include"]
    assert include == [ASSIGNED_REF]
    assert "~DEFAULT_BRANCH" not in include and f"refs/heads/{DEFAULT_BRANCH}" not in include


# --- the hostile corpus -----------------------------------------------------


def include(ref):
    return {"ref_name": {"include": [ref], "exclude": []}}


HOSTILE = [
    # wildcard or directory-wide branch targeting
    ("wildcard-double-star", mutate(conditions=include("refs/heads/builder/**")), {"INCLUDE_NOT_EXACT_REF"}),
    ("wildcard-single-star", mutate(conditions=include("refs/heads/builder/*")), {"INCLUDE_NOT_EXACT_REF"}),
    ("wildcard-bare", mutate(conditions=include("*")), {"INCLUDE_NOT_EXACT_REF"}),
    ("wildcard-question", mutate(conditions=include("refs/heads/builder/n28-assignment-000?")), {"INCLUDE_NOT_EXACT_REF"}),
    ("wildcard-class", mutate(conditions=include("refs/heads/builder/[an]28")), {"INCLUDE_NOT_EXACT_REF"}),
    ("all-refs", mutate(conditions=include("~ALL")), {"INCLUDE_NOT_EXACT_REF"}),
    ("prefix-glob-unqualified", mutate(conditions=include("builder/**")), {"INCLUDE_NOT_EXACT_REF"}),
    # default-branch targeting
    ("default-branch-token", mutate(conditions=include("~DEFAULT_BRANCH")), {"INCLUDE_DEFAULT_BRANCH"}),
    ("default-branch-qualified", mutate(conditions=include("refs/heads/main")), {"INCLUDE_DEFAULT_BRANCH"}),
    ("default-branch-short", mutate(conditions=include("main")), {"INCLUDE_DEFAULT_BRANCH"}),
    # tag targeting
    ("tag-target", mutate(target="tag"), {"TARGET_NOT_BRANCH"}),
    ("push-target", mutate(target="push"), {"TARGET_NOT_BRANCH"}),
    ("tag-ref", mutate(conditions=include("refs/tags/v0.1.1")), {"INCLUDE_TAG_REF"}),
    # missing or additional bypass actors
    ("bypass-empty", mutate(bypass_actors=[]), {"BYPASS_NOT_EXACTLY_ONE"}),
    ("bypass-null", mutate(bypass_actors=None), {"BYPASS_NOT_EXACTLY_ONE"}),
    (
        "bypass-two-actors",
        mutate(bypass_actors=[
            {"actor_id": RESOLVED_ACTOR_ID, "actor_type": "Integration", "bypass_mode": "always"},
            {"actor_id": 5, "actor_type": "Integration", "bypass_mode": "always"},
        ]),
        {"BYPASS_NOT_EXACTLY_ONE"},
    ),
    # non-Integration bypass actors
    ("bypass-user", mutate(bypass_actors__0__actor_type="User"), {"BYPASS_ACTOR_TYPE_NOT_INTEGRATION"}),
    ("bypass-team", mutate(bypass_actors__0__actor_type="Team"), {"BYPASS_ACTOR_TYPE_NOT_INTEGRATION"}),
    ("bypass-org-admin", mutate(bypass_actors__0__actor_type="OrganizationAdmin"), {"BYPASS_ACTOR_TYPE_NOT_INTEGRATION"}),
    ("bypass-repo-role", mutate(bypass_actors__0__actor_type="RepositoryRole"), {"BYPASS_ACTOR_TYPE_NOT_INTEGRATION"}),
    ("bypass-deploy-key", mutate(bypass_actors__0__actor_type="DeployKey"), {"BYPASS_ACTOR_TYPE_NOT_INTEGRATION"}),
    # bypass mode and actor id
    ("bypass-mode-pull-request", mutate(bypass_actors__0__bypass_mode="pull_request"), {"BYPASS_MODE_NOT_ALWAYS"}),
    ("bypass-mode-exempt", mutate(bypass_actors__0__bypass_mode="exempt"), {"BYPASS_MODE_NOT_ALWAYS"}),
    ("actor-id-placeholder", mutate(bypass_actors__0__actor_id=ACTOR_ID_PLACEHOLDER), {"BYPASS_ACTOR_ID_INVALID"}),
    ("actor-id-string-digits", mutate(bypass_actors__0__actor_id="424242"), {"BYPASS_ACTOR_ID_INVALID"}),
    ("actor-id-null", mutate(bypass_actors__0__actor_id=None), {"BYPASS_ACTOR_ID_INVALID"}),
    ("actor-id-zero", mutate(bypass_actors__0__actor_id=0), {"BYPASS_ACTOR_ID_INVALID"}),
    ("actor-id-negative", mutate(bypass_actors__0__actor_id=-1), {"BYPASS_ACTOR_ID_INVALID"}),
    ("actor-id-bool", mutate(bypass_actors__0__actor_id=True), {"BYPASS_ACTOR_ID_INVALID"}),
    # missing creation/update/deletion restrictions
    ("rules-no-creation", mutate(rules=[{"type": "update"}, {"type": "deletion"}]), {"RULES_NOT_EXACT_SET"}),
    ("rules-no-update", mutate(rules=[{"type": "creation"}, {"type": "deletion"}]), {"RULES_NOT_EXACT_SET"}),
    ("rules-no-deletion", mutate(rules=[{"type": "creation"}, {"type": "update"}]), {"RULES_NOT_EXACT_SET"}),
    ("rules-empty", mutate(rules=[]), {"RULES_NOT_EXACT_SET"}),
    ("rules-duplicated", mutate(rules=[{"type": "creation"}, {"type": "update"}, {"type": "update"}]), {"RULES_NOT_EXACT_SET"}),
    (
        "rules-widened",
        mutate(rules=[{"type": "creation"}, {"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}]),
        {"RULES_NOT_EXACT_SET"},
    ),
    ("rules-malformed", mutate(rules=["creation", "update", "deletion"]), {"RULES_NOT_EXACT_SET"}),
    # malformed or non-exact assigned refs
    ("ref-unqualified", mutate(conditions=include("builder/n28-assignment-0001")), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-empty", mutate(conditions=include("")), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-prefix-only", mutate(conditions=include("refs/heads/")), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-trailing-slash", mutate(conditions=include(ASSIGNED_REF + "/")), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-double-slash", mutate(conditions=include("refs/heads/builder//n28")), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-traversal", mutate(conditions=include("refs/heads/builder/../main")), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-space", mutate(conditions=include("refs/heads/builder/n28 assignment")), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-reflog-suffix", mutate(conditions=include("refs/heads/builder/n28@{0}")), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-lock-suffix", mutate(conditions=include(ASSIGNED_REF + ".lock")), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-control-character", mutate(conditions=include("refs/heads/builder/n28\n")), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-not-a-string", mutate(conditions=include(None)), {"INCLUDE_NOT_EXACT_REF"}),
    ("ref-none-included", mutate(conditions={"ref_name": {"include": [], "exclude": []}}), {"INCLUDE_NOT_EXACTLY_ONE"}),
    (
        "ref-two-included",
        mutate(conditions={"ref_name": {"include": [ASSIGNED_REF, "refs/heads/builder/n28-assignment-0002"], "exclude": []}}),
        {"INCLUDE_NOT_EXACTLY_ONE"},
    ),
    (
        "exclude-widens",
        mutate(conditions={"ref_name": {"include": [ASSIGNED_REF], "exclude": ["refs/heads/builder/n28-assignment-0001"]}}),
        {"EXCLUDE_NOT_EMPTY"},
    ),
    ("conditions-missing", mutate(conditions={}), {"CONDITIONS_MALFORMED"}),
    ("conditions-not-a-list", mutate(conditions={"ref_name": {"include": ASSIGNED_REF}}), {"CONDITIONS_MALFORMED"}),
    # enforcement, naming and envelope
    ("enforcement-evaluate", mutate(enforcement="evaluate"), {"ENFORCEMENT_NOT_ACTIVE"}),
    ("enforcement-disabled", mutate(enforcement="disabled"), {"ENFORCEMENT_NOT_ACTIVE"}),
    ("name-empty", mutate(name="  "), {"NAME_MISSING"}),
    ("name-collides-with-main-protection", mutate(name="main-protection"), {"NAME_RESERVED"}),
    ("extra-top-level-key", mutate(source_type="Organization"), {"EXTRA_TOP_LEVEL_KEY"}),
]


@pytest.mark.parametrize(
    "payload, expected", [pytest.param(p, e, id=i) for i, p, e in HOSTILE]
)
def test_hostile_payload_is_rejected_with_its_exact_reason(payload, expected):
    assert validate(payload) == expected


def test_hostile_corpus_covers_every_required_rejection_class():
    ids = {entry[0] for entry in HOSTILE}
    assert len(ids) == len(HOSTILE)
    covered = set().union(*(entry[2] for entry in HOSTILE))
    assert {
        "INCLUDE_NOT_EXACT_REF",
        "INCLUDE_DEFAULT_BRANCH",
        "INCLUDE_TAG_REF",
        "TARGET_NOT_BRANCH",
        "BYPASS_NOT_EXACTLY_ONE",
        "BYPASS_ACTOR_TYPE_NOT_INTEGRATION",
        "RULES_NOT_EXACT_SET",
    } <= covered


def test_non_object_payloads_are_refused():
    for payload in (None, [], "ruleset", 7):
        assert validate(payload) == {"PAYLOAD_NOT_OBJECT"}


def test_every_reason_code_is_documented():
    emitted = set().union(*(entry[2] for entry in HOSTILE)) | {
        "PAYLOAD_NOT_OBJECT", "BYPASS_MODE_NOT_ALWAYS", "EXCLUDE_NOT_EMPTY", "NAME_MISSING",
        "BYPASS_ACTOR_ID_INVALID", "EXTRA_TOP_LEVEL_KEY", "NAME_RESERVED",
        "CONDITIONS_MALFORMED", "ENFORCEMENT_NOT_ACTIVE", "INCLUDE_NOT_EXACTLY_ONE",
    }
    for code in emitted:
        assert f"`{code}`" in DOC, code


def test_mutation_helper_never_mutates_the_shipped_payload():
    mutate(enforcement="disabled", bypass_actors__0__actor_type="User")
    assert json.loads(RAW_PAYLOAD) == json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    assert resolved()["bypass_actors"][0]["actor_type"] == "Integration"
