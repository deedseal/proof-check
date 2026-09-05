"""rules/v0 evaluation, receipt production, and offline receipt verification.

``evaluate`` turns one policy and one bundle of already-collected evidence into
a verdict. It performs no I/O: whoever collects GitHub data hands it over as an
``Evidence`` value. ``build_receipt`` binds an evaluation into a
``proof-check-receipt/v1`` document with its digest. ``verify_receipt`` checks
a receipt offline: schema closure, canonical consistency, digests, and that the
verdict follows from the receipt's own recorded content.

rules/v0 in one paragraph: ``SCOPE_ESCAPE`` is the only ``FAIL``. Missing,
incomplete, or ambiguous input and coordinate drift are ``INDETERMINATE``.
Reserved codes are never emitted. ``PASS`` carries no reason codes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from proof_check.canonical import CanonicalizationError, canonical_bytes, canonical_sha256, sha256_hex
from proof_check.contracts import (
    POLICY_SCHEMA_VERSION,
    RECEIPT_SCHEMA_VERSION,
    RESERVED_REASON_CODES,
    RULES_VERSION,
    TOOL_NAME,
    TOOL_VERSION,
    ChangedPathRecord,
    ChangedPaths,
    CheckObservation,
    ContractError,
    DetailCode,
    Observation,
    Observations,
    PolicyDocument,
    PolicyRecord,
    ReasonCode,
    Receipt,
    ReviewObservation,
    SchemaViolation,
    SourceType,
    Subject,
    TargetKind,
    Tool,
    Trust,
    Verdict,
    Verification,
    _build,
    validate_receipt,
)
from proof_check.scope import (
    PathRefusal,
    ScopeRefusal,
    changed_path_set,
    compile_scope,
    outside_paths,
    parse_manifest,
    parse_pr_body_allowlist,
)

__all__ = [
    "CHANGED_FILES_CEILING",
    "ConfigurationError",
    "Declaration",
    "Evaluation",
    "Evidence",
    "ChangedPathsObservation",
    "VerificationResult",
    "build_receipt",
    "evaluate",
    "receipt_digest",
    "verify_receipt",
]

#: GitHub lists at most this many files for one pull request. A list this long
#: cannot be told apart from a truncated one, so it counts as incomplete.
CHANGED_FILES_CEILING = 3000

VERIFY_COMMAND = "proof-check verify proof-check-receipt.json --offline-bundle ."

DOES_NOT_PROVE: tuple[str, ...] = (
    "That the runner was honest: GitHub Actions job truth is GitHub-owned, and a receipt cannot prove otherwise.",
    "That the provider behind a green status actually tested the bytes.",
    "Current GitHub state, when verified offline.",
    "Anything about the correctness, quality, or authorization of the change.",
    "A receipt does not remove trust in GitHub, repository permissions, the evaluated inputs, the runner, or the reader's chosen verifier.",
)


class ConfigurationError(ContractError):
    """The policy is invalid for this evaluation (a CLI maps this to exit 2)."""


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class Declaration:
    """The scope declaration as collected, before any interpretation.

    ``text`` is the raw declaration (PR body, manifest file, or pinned input)
    or ``None`` when it could not be obtained. ``bound_sha`` is the commit the
    bytes were read at, when the source is a repository file. ``locator`` is a
    public URL for the receipt, or ``None``.
    """

    text: str | None = None
    bound_sha: str | None = None
    locator: str | None = None


@dataclass(frozen=True, slots=True)
class ChangedPathsObservation:
    api_reported_count: int
    records: tuple[ChangedPathRecord, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything one evaluation may look at. Collected elsewhere; consumed here."""

    subject: Subject
    observation: Observation
    changed_paths: ChangedPathsObservation
    declaration: Declaration
    observed_head_sha: str | None
    checks: tuple[CheckObservation, ...] = ()
    reviews: tuple[ReviewObservation, ...] = ()
    pr_author: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        return _build(cls, data, cls.__name__)


# --------------------------------------------------------------------------- outputs


@dataclass(frozen=True, slots=True)
class Evaluation:
    verdict: Verdict
    reason_codes: tuple[ReasonCode, ...]
    detail_codes: tuple[DetailCode, ...]
    outside_paths: tuple[str, ...] | None
    explanation: str
    context: tuple[str, ...]
    policy: PolicyRecord
    path_set: tuple[str, ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class VerificationResult:
    ok: bool
    findings: tuple[str, ...]
    receipt: Receipt | None = None


# --------------------------------------------------------------------------- evaluation


@dataclass
class _State:
    reasons: list[ReasonCode] = field(default_factory=list)
    details: list[DetailCode] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)

    def indeterminate(self, reason: ReasonCode, message: str, detail: DetailCode | None = None) -> None:
        assert reason not in RESERVED_REASON_CODES
        if reason not in self.reasons:
            self.reasons.append(reason)
        if detail is not None and detail not in self.details:
            self.details.append(detail)
        self.problems.append(message)


def _resolve_declaration(policy: PolicyDocument, evidence: Evidence, state: _State) -> tuple[PolicyRecord, tuple[str, ...] | None]:
    """Interpret the policy's scope source against the collected declaration.

    Returns the receipt's policy record and the accepted entry list, or
    ``None`` for the entries when the scope cannot be evaluated (the state
    then carries the ``INDETERMINATE`` reason).
    """
    scope = policy.scope
    decl = evidence.declaration
    policy_digest = canonical_sha256(policy.to_dict())
    kind = scope.source_kind()
    trust = scope.trust

    def record(source_type: SourceType, raw: str | None, entries: tuple[str, ...]) -> PolicyRecord:
        raw_sha = None if raw is None else sha256_hex(raw.encode("utf-8"))
        return PolicyRecord(source_type, decl.locator, raw_sha, entries, trust, policy_digest)

    if trust is Trust.NONE:
        state.indeterminate(ReasonCode.EVIDENCE_MISSING, "the policy declares no trusted scope (trust: none), so there is no rule to hold the change against")
        return record(SourceType.NONE, None, ()), None

    if scope.entries is not None:
        if not scope.entries:
            raise ConfigurationError("scope.entries is present but empty; an empty declaration fails closed")
        raw = "\n".join(scope.entries)
        return _accept(record(SourceType.OWNER_PINNED_INPUT, raw, tuple(scope.entries)), state)

    if kind == "pr_body_allowlist":
        if trust is Trust.BLOCKING:
            raise ConfigurationError("an author-controlled PR body cannot be a blocking scope source; use file: or pinned:")
        if decl.text is None:
            state.indeterminate(ReasonCode.EVIDENCE_MISSING, "the pull request body could not be read, so the declared scope is unknown")
            return record(SourceType.AUTHOR_PR_BODY, None, ()), None
        try:
            entries = parse_pr_body_allowlist(decl.text)
        except ScopeRefusal as refusal:
            state.indeterminate(ReasonCode.EVIDENCE_AMBIGUOUS, f"the declared scope is refused ({refusal.code}): {refusal}")
            return record(SourceType.AUTHOR_PR_BODY, decl.text, ()), None
        return _accept(record(SourceType.AUTHOR_PR_BODY, decl.text, entries), state)

    if kind == "file":
        source_type = SourceType.TRUSTED_BASE_MANIFEST
        if decl.text is None:
            state.indeterminate(ReasonCode.EVIDENCE_MISSING, f"the scope manifest {scope.source_argument()!r} is absent from the trusted base")
            return record(source_type, None, ()), None
        if decl.bound_sha is None:
            state.indeterminate(ReasonCode.EVIDENCE_AMBIGUOUS, "the scope manifest was read from a mutable ref without a frozen commit hash")
            return record(source_type, decl.text, ()), None
        if decl.bound_sha != evidence.subject.base_sha:
            state.indeterminate(ReasonCode.EVIDENCE_MISSING, f"the scope manifest is bound to {decl.bound_sha}, not to the base commit {evidence.subject.base_sha}")
            return record(source_type, decl.text, ()), None
        return _parse_manifest(record, source_type, decl.text, state)

    if kind == "pinned":
        source_type = SourceType.OWNER_PINNED_INPUT
        if decl.text is None:
            state.indeterminate(ReasonCode.EVIDENCE_MISSING, "the pinned scope declaration was not provided")
            return record(source_type, None, ()), None
        actual = sha256_hex(decl.text.encode("utf-8"))
        if actual != scope.source_argument():
            state.indeterminate(ReasonCode.EVIDENCE_AMBIGUOUS, f"the scope declaration bytes digest to {actual}, not to the pinned {scope.source_argument()}")
            return record(source_type, decl.text, ()), None
        return _parse_manifest(record, source_type, decl.text, state)

    raise ConfigurationError(f"unsupported scope source {scope.source!r}")


def _parse_manifest(record, source_type: SourceType, text: str, state: _State):
    try:
        entries = parse_manifest(text)
    except ScopeRefusal as refusal:
        state.indeterminate(ReasonCode.EVIDENCE_AMBIGUOUS, f"the declared scope is refused ({refusal.code}): {refusal}")
        return record(source_type, text, ()), None
    return _accept(record(source_type, text, entries), state)


def _accept(policy_record: PolicyRecord, state: _State) -> tuple[PolicyRecord, tuple[str, ...] | None]:
    try:
        compile_scope(policy_record.allowed_entries)
    except ScopeRefusal as refusal:
        state.indeterminate(ReasonCode.EVIDENCE_AMBIGUOUS, f"the declared scope is refused ({refusal.code}): {refusal}")
        return policy_record, None
    return policy_record, policy_record.allowed_entries


def _check_subject_coordinates(subject: Subject, state: _State) -> None:
    if subject.target_kind is TargetKind.HEAD:
        expected = subject.head_sha
    else:
        expected = subject.merge_or_group_sha
    if expected is None:
        state.indeterminate(ReasonCode.EVIDENCE_AMBIGUOUS, f"target_kind {subject.target_kind} needs a merge_or_group_sha, none was recorded")
    elif subject.evaluated_sha != expected:
        state.indeterminate(ReasonCode.EVIDENCE_AMBIGUOUS, f"evaluated_sha {subject.evaluated_sha} does not match the {subject.target_kind} coordinate {expected}")


def _check_drift(evidence: Evidence, state: _State) -> None:
    observed = evidence.observed_head_sha
    if observed is None:
        state.indeterminate(ReasonCode.EVIDENCE_MISSING, "the head could not be re-read after collecting evidence (for example, a fork head that is unavailable)")
    elif observed != evidence.subject.head_sha:
        state.indeterminate(
            ReasonCode.STALE_HEAD,
            f"the head moved from {evidence.subject.head_sha} to {observed} during evaluation",
            DetailCode.COORDINATE_DRIFT,
        )


def _changed_set_complete(changed: ChangedPathsObservation) -> bool:
    received = len(changed.records)
    return (
        not changed.truncated
        and received == changed.api_reported_count
        and changed.api_reported_count < CHANGED_FILES_CEILING
    )


def _check_changed_paths(changed: ChangedPathsObservation, state: _State) -> tuple[str, ...]:
    if not _changed_set_complete(changed):
        state.indeterminate(
            ReasonCode.EVIDENCE_MISSING,
            f"the changed-path set is incomplete: GitHub reports {changed.api_reported_count} files, {len(changed.records)} received"
            + (", list truncated" if changed.truncated else "")
            + (f", ceiling of {CHANGED_FILES_CEILING} reached" if changed.api_reported_count >= CHANGED_FILES_CEILING else ""),
            DetailCode.CHANGED_PATH_SET_INCOMPLETE,
        )
    try:
        return changed_path_set((r.filename, r.previous_filename) for r in changed.records)
    except PathRefusal as refusal:
        state.indeterminate(ReasonCode.EVIDENCE_AMBIGUOUS, f"a changed path cannot be matched honestly ({refusal.code}): {refusal}")
        return ()


def _note_context(evidence: Evidence, state: _State) -> None:
    subject = evidence.subject
    bound: dict[str, set[str]] = {}
    for check in evidence.checks:
        bound.setdefault(check.head_sha, set()).add("check")
    for review in evidence.reviews:
        bound.setdefault(review.commit_id, set()).add("review")
    labels = {subject.head_sha: "head"}
    if subject.merge_or_group_sha is not None:
        labels[subject.merge_or_group_sha] = str(subject.target_kind) if subject.target_kind is not TargetKind.HEAD else "merge"
    if len(bound) > 1:
        described = ", ".join(f"{sha} ({labels.get(sha, 'other')}: {'+'.join(sorted(kinds))})" for sha, kinds in sorted(bound.items()))
        state.context.append(
            f"Evidence binds to {len(bound)} distinct commit SHAs: {described}. rules/v0 records this verbatim as context and does not judge it; equality across head, test-merge and merge-group coordinates is not required."
        )
    if evidence.pr_author is not None and any(r.actor == evidence.pr_author for r in evidence.reviews):
        state.context.append(
            f"The pull request author {evidence.pr_author!r} also appears as a reviewer. rules/v0 has no rule on producer and reviewer identity; this is recorded as context only."
        )


def evaluate(policy: PolicyDocument | Mapping[str, Any], evidence: Evidence) -> Evaluation:
    """Apply rules/v0 to one policy and one bundle of evidence. Pure; no I/O."""
    state = _State()
    if isinstance(policy, Mapping):
        version = policy.get("schema_version")
        if version != POLICY_SCHEMA_VERSION:
            return _unknown_policy_version(policy, evidence, version)
        policy = PolicyDocument.from_dict(policy)

    _check_subject_coordinates(evidence.subject, state)
    policy_record, entries = _resolve_declaration(policy, evidence, state)
    _check_drift(evidence, state)
    complete = _changed_set_complete(evidence.changed_paths)
    path_set = _check_changed_paths(evidence.changed_paths, state)
    _note_context(evidence, state)

    if state.reasons:
        verdict = Verdict.INDETERMINATE
        outside = None
        explanation = "The tool cannot say whether the change stays inside its declared scope: " + " ".join(f"{p}." for p in state.problems)
    else:
        assert entries is not None
        outside = outside_paths(compile_scope(entries), path_set)
        if outside:
            verdict = Verdict.FAIL
            state.reasons.append(ReasonCode.SCOPE_ESCAPE)
            state.details.append(DetailCode.PATH_OUTSIDE_DECLARED_SCOPE)
            shown = ", ".join(outside[:5]) + (f", and {len(outside) - 5} more" if len(outside) > 5 else "")
            explanation = (
                f"The change touched {len(outside)} of {len(path_set)} paths outside its declared scope at commit {evidence.subject.evaluated_sha}: {shown}."
            )
        else:
            verdict = Verdict.PASS
            outside = None
            state.details.append(DetailCode.ALL_CHANGED_PATHS_WITHIN_DECLARED_SCOPE)
            subject = evidence.subject
            if subject.target_kind is TargetKind.MERGE_GROUP and subject.merge_or_group_sha == subject.evaluated_sha:
                state.details.append(DetailCode.MERGE_GROUP_COORDINATES_COHERENT)
            explanation = (
                f"All {len(path_set)} changed paths stay inside the declared scope at commit {subject.evaluated_sha}"
                f" as observed at {evidence.observation.cutoff_utc}. Nothing more is claimed."
            )
    if state.context:
        explanation += " Context: " + " ".join(state.context)

    assert not any(code in RESERVED_REASON_CODES for code in state.reasons)
    assert verdict is not Verdict.PASS or not state.reasons
    return Evaluation(
        verdict=verdict,
        reason_codes=tuple(state.reasons),
        detail_codes=tuple(state.details),
        outside_paths=outside,
        explanation=explanation,
        context=tuple(state.context),
        policy=policy_record,
        path_set=path_set,
        complete=complete,
    )


def _unknown_policy_version(policy: Mapping[str, Any], evidence: Evidence, version: Any) -> Evaluation:
    try:
        digest = canonical_sha256(dict(policy))
    except CanonicalizationError:
        digest = sha256_hex(json.dumps(policy, sort_keys=True, default=str).encode("utf-8"))
    record = PolicyRecord(SourceType.NONE, None, None, (), Trust.NONE, digest)
    message = f"the policy schema_version {version!r} is unknown to this tool; only {POLICY_SCHEMA_VERSION} is understood"
    return Evaluation(
        verdict=Verdict.INDETERMINATE,
        reason_codes=(ReasonCode.EVIDENCE_AMBIGUOUS,),
        detail_codes=(),
        outside_paths=None,
        explanation=f"The tool cannot say whether the change stays inside its declared scope: {message}.",
        context=(),
        policy=record,
        path_set=(),
        complete=_changed_set_complete(evidence.changed_paths),
    )


# --------------------------------------------------------------------------- receipts


def _portable(text: str) -> str:
    """Return ``text`` unchanged when it is valid Unicode, else an ASCII-escaped form that canonicalizes."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("ascii")


def _path_array_digest(records: tuple[ChangedPathRecord, ...]) -> str:
    paths: set[str] = set()
    for r in records:
        paths.add(_portable(r.filename))
        if r.previous_filename is not None:
            paths.add(_portable(r.previous_filename))
    return canonical_sha256(sorted(paths))


def _portable_records(records: tuple[ChangedPathRecord, ...]) -> tuple[ChangedPathRecord, ...]:
    portable = [
        ChangedPathRecord(_portable(r.filename), r.status, None if r.previous_filename is None else _portable(r.previous_filename))
        for r in records
    ]
    return tuple(sorted(portable, key=lambda r: (r.filename, r.previous_filename or "", r.status)))


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical bytes of the receipt object with ``receipt_digest`` removed."""
    body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    return canonical_sha256(body)


def build_receipt(
    evaluation: Evaluation,
    evidence: Evidence,
    source_commit: str,
    verifier_locator: str | None = None,
    verifier_sha256: str | None = None,
) -> Receipt:
    """Bind an evaluation to its evidence as a schema-valid ``proof-check-receipt/v1``."""
    subject = evidence.subject
    changed = evidence.changed_paths
    records = _portable_records(changed.records)
    limitations = [
        f"The verdict binds only to {subject.evaluated_sha} ({subject.target_kind}) at cutoff {evidence.observation.cutoff_utc}; nothing after that instant is claimed.",
        "PASS means evidence coherence under the declared rules at the named cutoff. Nothing more.",
        "Offline verification proves canonical consistency, schema closure and digests only; it never claims current GitHub state.",
    ]
    if evaluation.policy.source_type is SourceType.AUTHOR_PR_BODY:
        limitations.append("The declared scope is author-controlled and therefore advisory evidence, never security policy.")
    if any(_portable(r.filename) != r.filename or (r.previous_filename and _portable(r.previous_filename) != r.previous_filename) for r in changed.records):
        limitations.append("One or more changed paths were not valid Unicode and are recorded in an ASCII-escaped form.")
    limitations.extend(evaluation.context)

    identity = [subject.repository, subject.pr_number, subject.evaluated_sha, evidence.observation.cutoff_utc, evidence.observation.watermark]
    body: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": "pcr1-" + canonical_sha256(identity)[:32],
        "subject": subject.to_dict(),
        "observation": evidence.observation.to_dict(),
        "policy": evaluation.policy.to_dict(),
        "tool": Tool(TOOL_NAME, TOOL_VERSION, source_commit, RULES_VERSION).to_dict(),
        "observations": Observations(
            checks=tuple(sorted(evidence.checks, key=lambda c: c.id)),
            reviews=tuple(sorted(evidence.reviews, key=lambda r: r.id)),
            changed_paths=ChangedPaths(
                api_reported_count=changed.api_reported_count,
                received_count=len(changed.records),
                complete=evaluation.complete,
                canonical_path_array_sha256=_path_array_digest(changed.records),
                records=records,
            ),
        ).to_dict(),
        "verdict": evaluation.verdict.value,
        "reason_codes": [code.value for code in evaluation.reason_codes],
        "detail_codes": [code.value for code in evaluation.detail_codes],
        "limitations": limitations,
        "does_not_prove": list(DOES_NOT_PROVE),
        "verification": Verification("offline", VERIFY_COMMAND, 0, verifier_locator, verifier_sha256).to_dict(),
    }
    if evaluation.outside_paths is not None:
        body["outside_paths"] = list(evaluation.outside_paths)
    body["receipt_digest"] = receipt_digest(body)
    return Receipt.from_dict(body)


# --------------------------------------------------------------------------- offline verification


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def verify_receipt(receipt: bytes | str | Mapping[str, Any], policy: Mapping[str, Any] | None = None) -> VerificationResult:
    """Verify a receipt offline.

    Checks, in order: the bytes parse as JSON without duplicate keys; the
    document conforms to the receipt schema; ``receipt_digest`` matches the
    canonical bytes; ``canonical_path_array_sha256`` matches the recorded
    records; no reserved reason code is present; ordering rules hold; the
    verdict follows from the receipt's own recorded content; and, when the
    policy document is supplied, ``policy_digest`` matches it. Nothing about
    current GitHub state is claimed.
    """
    findings: list[str] = []
    if isinstance(receipt, (bytes, str)):
        try:
            data = json.loads(receipt, object_pairs_hook=_reject_duplicate_keys)
        except (ValueError, UnicodeDecodeError) as error:
            return VerificationResult(False, (f"malformed receipt: {error}",))
    else:
        data = dict(receipt)
    if not isinstance(data, dict):
        return VerificationResult(False, ("malformed receipt: the document is not a JSON object",))

    try:
        validate_receipt(data)
    except SchemaViolation as violation:
        findings.extend(f"schema: {pointer or '<root>'}: {message}" for pointer, message in violation.findings)
        return VerificationResult(False, tuple(findings))
    model = Receipt.from_dict(data)

    try:
        expected = receipt_digest(data)
    except CanonicalizationError as error:
        return VerificationResult(False, (f"receipt cannot be canonicalized: {error}",))
    if expected != data["receipt_digest"]:
        findings.append(f"receipt_digest mismatch: recorded {data['receipt_digest']}, canonical bytes digest to {expected}")

    changed = model.observations.changed_paths
    if _path_array_digest(changed.records) != changed.canonical_path_array_sha256:
        findings.append("canonical_path_array_sha256 does not match the recorded changed-path records")
    if changed.received_count != len(changed.records):
        findings.append(f"received_count {changed.received_count} does not match {len(changed.records)} recorded records")
    honest_complete = not changed.complete or (changed.received_count == changed.api_reported_count and changed.api_reported_count < CHANGED_FILES_CEILING)
    if not honest_complete:
        findings.append("changed_paths.complete is true although counts or the ceiling say otherwise")

    reserved = [code for code in model.reason_codes if code in RESERVED_REASON_CODES]
    if reserved:
        findings.append(f"reserved reason codes emitted: {[c.value for c in reserved]}")

    ids = [c.id for c in model.observations.checks]
    if ids != sorted(ids):
        findings.append("observations.checks are not ordered by id ascending")
    ids = [r.id for r in model.observations.reviews]
    if ids != sorted(ids):
        findings.append("observations.reviews are not ordered by id ascending")
    if model.outside_paths is not None and list(model.outside_paths) != sorted(set(model.outside_paths)):
        findings.append("outside_paths is not sorted")

    findings.extend(_verdict_follows(model))

    if policy is not None:
        try:
            digest = canonical_sha256(dict(policy))
        except CanonicalizationError as error:
            digest = None
            findings.append(f"the supplied policy cannot be canonicalized: {error}")
        if digest is not None and digest != model.policy.policy_digest:
            findings.append(f"policy_digest mismatch: recorded {model.policy.policy_digest}, supplied policy digests to {digest}")

    return VerificationResult(not findings, tuple(findings), model)


def _verdict_follows(model: Receipt) -> list[str]:
    """Re-derive what the receipt's own content permits and compare with its verdict."""
    findings: list[str] = []
    changed = model.observations.changed_paths
    subject = model.subject
    coordinate = subject.head_sha if subject.target_kind is TargetKind.HEAD else subject.merge_or_group_sha
    coherent = coordinate is not None and subject.evaluated_sha == coordinate

    scope_ok = True
    try:
        scope = compile_scope(model.policy.allowed_entries)
    except ScopeRefusal:
        scope = None
        scope_ok = False
    try:
        paths = changed_path_set((r.filename, r.previous_filename) for r in changed.records)
    except PathRefusal:
        paths = None

    decidable = (
        changed.complete
        and coherent
        and scope_ok
        and paths is not None
        and model.policy.source_type is not SourceType.NONE
        and model.policy.trust is not Trust.NONE
    )
    if model.verdict is Verdict.INDETERMINATE:
        return findings
    if not decidable:
        findings.append(f"verdict {model.verdict} is not supported by the receipt's own content; the recorded evidence only permits INDETERMINATE")
        return findings
    assert scope is not None and paths is not None
    outside = outside_paths(scope, paths)
    if model.verdict is Verdict.PASS and outside:
        findings.append(f"verdict PASS although {len(outside)} recorded paths fall outside the recorded scope")
    if model.verdict is Verdict.FAIL and tuple(model.outside_paths or ()) != outside:
        findings.append("outside_paths does not match the paths outside the recorded scope")
    if model.verdict is Verdict.FAIL and DetailCode.PATH_OUTSIDE_DECLARED_SCOPE not in model.detail_codes:
        findings.append("FAIL without detail PATH_OUTSIDE_DECLARED_SCOPE")
    if model.verdict is Verdict.PASS and DetailCode.ALL_CHANGED_PATHS_WITHIN_DECLARED_SCOPE not in model.detail_codes:
        findings.append("PASS without detail ALL_CHANGED_PATHS_WITHIN_DECLARED_SCOPE")
    return findings
