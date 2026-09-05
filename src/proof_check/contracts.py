"""Typed models for the frozen contract plus JSON Schema validation.

The schemas under ``schemas/`` are the authority. The dataclasses here mirror
them one to one so the rest of the core can work with typed values; every
model is validated against its schema when loaded from a mapping and again
when a receipt is produced. Closed vocabularies (verdicts, reason codes,
detail codes) are enumerations whose members are exactly the schema enums.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import types
import typing
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Mapping, TypeVar

from jsonschema import Draft202012Validator

__all__ = [
    "ChangedPathRecord",
    "ChangedPaths",
    "CheckObservation",
    "ChecksConfig",
    "ContractError",
    "DetailCode",
    "Observation",
    "POLICY_SCHEMA_VERSION",
    "PolicyDocument",
    "PolicyRecord",
    "RECEIPT_SCHEMA_VERSION",
    "RESERVED_REASON_CODES",
    "RULES_VERSION",
    "ReasonCode",
    "Receipt",
    "ReceiptConfig",
    "ReviewObservation",
    "ReviewsConfig",
    "SchemaViolation",
    "ScopeConfig",
    "SourceType",
    "Subject",
    "TOOL_NAME",
    "TOOL_VERSION",
    "TargetKind",
    "Tool",
    "Trust",
    "Verdict",
    "VerdictPolicy",
    "Verification",
    "load_schema",
    "validate_policy",
    "validate_receipt",
]

RECEIPT_SCHEMA_VERSION = "proof-check-receipt/v1"
POLICY_SCHEMA_VERSION = "proof-check-policy/v1"
RULES_VERSION = "rules/v0"
TOOL_NAME = "proof-check"
TOOL_VERSION = "0.0.0"

RECEIPT_SCHEMA_FILE = "proof-check-receipt.v1.schema.json"
POLICY_SCHEMA_FILE = "proof-check-policy.v1.schema.json"


class ContractError(ValueError):
    """Input does not conform to the frozen contract."""


class SchemaViolation(ContractError):
    """A document failed JSON Schema validation. ``findings`` lists (pointer, message)."""

    def __init__(self, schema_title: str, findings: list[tuple[str, str]]):
        self.schema_title = schema_title
        self.findings = findings
        lines = "; ".join(f"{pointer or '<root>'}: {message}" for pointer, message in findings)
        super().__init__(f"{schema_title}: {lines}")


class Verdict(enum.StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


class ReasonCode(enum.StrEnum):
    SCOPE_ESCAPE = "SCOPE_ESCAPE"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_AMBIGUOUS = "EVIDENCE_AMBIGUOUS"
    STALE_HEAD = "STALE_HEAD"
    MIXED_HEAD_EVIDENCE = "MIXED_HEAD_EVIDENCE"
    PRODUCER_REVIEWER_CONFLICT = "PRODUCER_REVIEWER_CONFLICT"


#: Reserved in rules/v0. Appearing in any emitted reason list is a defect.
RESERVED_REASON_CODES: frozenset[ReasonCode] = frozenset(
    {ReasonCode.MIXED_HEAD_EVIDENCE, ReasonCode.PRODUCER_REVIEWER_CONFLICT}
)


class DetailCode(enum.StrEnum):
    PATH_OUTSIDE_DECLARED_SCOPE = "PATH_OUTSIDE_DECLARED_SCOPE"
    ALL_CHANGED_PATHS_WITHIN_DECLARED_SCOPE = "ALL_CHANGED_PATHS_WITHIN_DECLARED_SCOPE"
    CHANGED_PATH_SET_INCOMPLETE = "CHANGED_PATH_SET_INCOMPLETE"
    COORDINATE_DRIFT = "COORDINATE_DRIFT"
    MERGE_GROUP_COORDINATES_COHERENT = "MERGE_GROUP_COORDINATES_COHERENT"


class TargetKind(enum.StrEnum):
    HEAD = "head"
    TEST_MERGE = "test_merge"
    MERGE_GROUP = "merge_group"


class Trust(enum.StrEnum):
    NONE = "none"
    ADVISORY = "advisory"
    BLOCKING = "blocking"


class SourceType(enum.StrEnum):
    NONE = "none"
    AUTHOR_PR_BODY = "author_pr_body"
    TRUSTED_BASE_MANIFEST = "trusted_base_manifest"
    OWNER_PINNED_INPUT = "owner_pinned_input"


# --------------------------------------------------------------------------- schemas


def _schema_dir() -> Path:
    """Locate ``schemas/``: next to the package (a bundled copy) or at the repository root."""
    here = Path(__file__).resolve()
    for candidate in (here.parent / "schemas", here.parents[2] / "schemas"):
        if (candidate / RECEIPT_SCHEMA_FILE).is_file() and (candidate / POLICY_SCHEMA_FILE).is_file():
            return candidate
    raise ContractError("schemas/ directory with the frozen contract schemas was not found")


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    """Load one schema file by name and check that it is itself a valid Draft 2020-12 schema."""
    path = _schema_dir() / name
    with path.open("rb") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return schema


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name))


def _validate(name: str, document: Any) -> None:
    validator = _validator(name)
    findings = sorted(
        ("/" + "/".join(str(part) for part in error.absolute_path), error.message)
        for error in validator.iter_errors(document)
    )
    if findings:
        raise SchemaViolation(load_schema(name)["title"], findings)


def validate_receipt(document: Any) -> None:
    """Raise ``SchemaViolation`` unless ``document`` conforms to proof-check-receipt/v1."""
    _validate(RECEIPT_SCHEMA_FILE, document)


def validate_policy(document: Any) -> None:
    """Raise ``SchemaViolation`` unless ``document`` conforms to proof-check-policy/v1."""
    _validate(POLICY_SCHEMA_FILE, document)


# --------------------------------------------------------------------------- model plumbing

_T = TypeVar("_T")


def _build(tp: Any, value: Any, where: str) -> Any:
    """Build a typed value from plain JSON data according to annotation ``tp``."""
    origin = typing.get_origin(tp)
    if origin in (types.UnionType, typing.Union):
        args = typing.get_args(tp)
        if value is None and type(None) in args:
            return None
        for arg in args:
            if arg is type(None):
                continue
            return _build(arg, value, where)
    if origin is tuple:
        (item_tp, _ellipsis) = typing.get_args(tp)
        if not isinstance(value, list):
            raise ContractError(f"{where}: expected an array")
        return tuple(_build(item_tp, item, f"{where}[{index}]") for index, item in enumerate(value))
    if dataclasses.is_dataclass(tp):
        if not isinstance(value, Mapping):
            raise ContractError(f"{where}: expected an object")
        known = {f.name for f in fields(tp)}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ContractError(f"{where}: unexpected members {unknown}")
        hints = typing.get_type_hints(tp)
        kwargs = {}
        for f in fields(tp):
            if f.name in value:
                kwargs[f.name] = _build(hints[f.name], value[f.name], f"{where}.{f.name}")
            elif f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                continue
            else:
                raise ContractError(f"{where}: missing member {f.name!r}")
        return tp(**kwargs)
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        try:
            return tp(value)
        except ValueError as error:
            raise ContractError(f"{where}: {value!r} is not one of {[m.value for m in tp]}") from error
    if tp is bool:
        if not isinstance(value, bool):
            raise ContractError(f"{where}: expected a boolean")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(f"{where}: expected an integer")
        return value
    if tp is str:
        if not isinstance(value, str):
            raise ContractError(f"{where}: expected a string")
        return value
    raise ContractError(f"{where}: unsupported annotation {tp!r}")


def _plain(value: Any) -> Any:
    """Convert a model tree to plain JSON data (tuples become lists, enums their values)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


class _Model:
    """Mixin: ``from_dict`` builds a typed instance, ``to_dict`` returns plain JSON data."""

    @classmethod
    def from_dict(cls: type[_T], data: Mapping[str, Any]) -> _T:
        return _build(cls, data, cls.__name__)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


# --------------------------------------------------------------------------- policy document


@dataclass(frozen=True, slots=True)
class ScopeConfig(_Model):
    source: str
    trust: Trust
    entries: tuple[str, ...] | None = None

    def source_kind(self) -> str:
        """``pr_body_allowlist``, ``file`` or ``pinned`` as the policy schema defines them."""
        if self.source == "pr_body_allowlist":
            return "pr_body_allowlist"
        return self.source.split(":", 1)[0]

    def source_argument(self) -> str:
        return self.source.split(":", 1)[1] if ":" in self.source else ""


@dataclass(frozen=True, slots=True)
class ChecksConfig(_Model):
    required_selectors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewsConfig(_Model):
    policy: str


@dataclass(frozen=True, slots=True)
class VerdictPolicy(_Model):
    fail_exit: int
    indeterminate_exit: int = 20


@dataclass(frozen=True, slots=True)
class ReceiptConfig(_Model):
    mode: str = "always"


@dataclass(frozen=True, slots=True)
class PolicyDocument(_Model):
    """One ``proof-check-policy/v1`` document."""

    schema_version: str
    scope: ScopeConfig
    checks: ChecksConfig
    reviews: ReviewsConfig
    verdict_policy: VerdictPolicy
    receipt: ReceiptConfig

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PolicyDocument":
        validate_policy(data)
        return _build(cls, data, cls.__name__)

    def to_dict(self) -> dict[str, Any]:
        plain = _plain(self)
        if self.scope.entries is None:
            del plain["scope"]["entries"]
        return plain


# --------------------------------------------------------------------------- receipt


@dataclass(frozen=True, slots=True)
class Subject(_Model):
    repository: str
    repository_id: int
    repository_visibility: str
    pr_number: int
    target_kind: TargetKind
    evaluated_sha: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    merge_or_group_sha: str | None


@dataclass(frozen=True, slots=True)
class Observation(_Model):
    cutoff_utc: str
    captured_at_utc: str
    watermark: str


@dataclass(frozen=True, slots=True)
class PolicyRecord(_Model):
    source_type: SourceType
    source_locator: str | None
    raw_sha256: str | None
    allowed_entries: tuple[str, ...]
    trust: Trust
    policy_digest: str


@dataclass(frozen=True, slots=True)
class Tool(_Model):
    name: str
    version: str
    source_commit: str
    rules_version: str


@dataclass(frozen=True, slots=True)
class CheckObservation(_Model):
    id: int
    suite_id: int
    name: str
    head_sha: str
    status: str
    conclusion: str | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class ReviewObservation(_Model):
    id: int
    actor: str
    association: str
    state: str
    submitted_at: str
    commit_id: str


@dataclass(frozen=True, slots=True)
class ChangedPathRecord(_Model):
    filename: str
    status: str
    previous_filename: str | None = None


@dataclass(frozen=True, slots=True)
class ChangedPaths(_Model):
    api_reported_count: int
    received_count: int
    complete: bool
    canonical_path_array_sha256: str
    records: tuple[ChangedPathRecord, ...]


@dataclass(frozen=True, slots=True)
class Observations(_Model):
    checks: tuple[CheckObservation, ...]
    reviews: tuple[ReviewObservation, ...]
    changed_paths: ChangedPaths


@dataclass(frozen=True, slots=True)
class Verification(_Model):
    mode: str
    command: str
    expected_exit: int
    verifier_locator: str | None
    verifier_sha256: str | None


@dataclass(frozen=True, slots=True)
class Receipt(_Model):
    """One ``proof-check-receipt/v1`` document, digest included."""

    schema_version: str
    receipt_id: str
    subject: Subject
    observation: Observation
    policy: PolicyRecord
    tool: Tool
    observations: Observations
    verdict: Verdict
    reason_codes: tuple[ReasonCode, ...]
    detail_codes: tuple[DetailCode, ...]
    limitations: tuple[str, ...]
    does_not_prove: tuple[str, ...]
    verification: Verification
    receipt_digest: str
    outside_paths: tuple[str, ...] | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Receipt":
        validate_receipt(data)
        return _build(cls, data, cls.__name__)

    def to_dict(self) -> dict[str, Any]:
        plain = _plain(self)
        if self.outside_paths is None:
            del plain["outside_paths"]
        return plain
