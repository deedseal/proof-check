"""Command-line adapter for GitHub observation and receipt verification."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Sequence, TextIO

import proof_check.contracts as contract_module
from proof_check.canonical import canonical_bytes, sha256_hex
from proof_check.contracts import (
    POLICY_SCHEMA_VERSION,
    PolicyDocument,
    Receipt,
    SchemaViolation,
    SourceType,
    Verdict,
    validate_policy,
)
from proof_check.github_read import ObservationUnavailable, Transport, collect_evidence, reobserve_pull_request
from proof_check.scope import ScopeRefusal, parse_manifest, parse_pr_body_allowlist
from proof_check.verify import ConfigurationError, build_receipt, evaluate, verify_receipt

__all__ = ["main", "resolve_source_commit"]

EXIT_PASS = 0
EXIT_FAIL = 10
EXIT_INDETERMINATE = 20
EXIT_INVALID = 2
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="proof-check", description="Check one pull request or verify a Proof Check receipt.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="observe and evaluate one pull request")
    check.add_argument("--repo", required=True, metavar="OWNER/NAME")
    check.add_argument("--pr", required=True, type=int, metavar="N")
    check.add_argument("--policy", required=True, metavar="FILE")
    check.add_argument("--receipt", required=True, metavar="OUT.JSON")
    check.add_argument("--target", choices=("head", "test_merge", "merge_group"), default="head")
    check.add_argument("--token-env", default="GITHUB_TOKEN", metavar="NAME")
    check.add_argument("--declaration", metavar="FILE")

    verify = subparsers.add_parser("verify", help="verify a receipt, optionally without network access")
    verify.add_argument("receipt", metavar="RECEIPT.JSON")
    verify.add_argument("--offline-bundle", metavar="DIR")
    verify.add_argument("--token-env", default="GITHUB_TOKEN", metavar="NAME")
    return parser


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(f"{label} is unreadable: {path}") from error
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is malformed JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"{label} is unreadable UTF-8: {path}") from error


def _git_commit(directory: Path, package_directory: Path | None = None) -> str | None:
    installed = Path(__file__).resolve().parent
    package_source = installed if package_directory is None else package_directory.resolve()
    try:
        root_text = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        root = Path(root_text).resolve()
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip().lower()
        installed_files = sorted(installed.glob("*.py"))
        if not installed_files:
            return None
        for installed_file in installed_files:
            tracked_file = package_source / installed_file.name
            relative = tracked_file.relative_to(root).as_posix()
            subprocess.run(
                ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
                check=True,
                capture_output=True,
            )
            committed = subprocess.run(
                ["git", "-C", str(root), "show", f"HEAD:{relative}"],
                check=True,
                capture_output=True,
            ).stdout
            if installed_file.read_bytes() != committed:
                return None
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None
    if not _SHA_RE.fullmatch(commit):
        return None
    return commit


def _direct_url_document() -> dict[str, Any] | None:
    try:
        distribution = importlib.metadata.distribution("proof-check")
        raw = distribution.read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def _direct_url_commit() -> str | None:
    document = _direct_url_document()
    if document is None:
        return None
    vcs = document.get("vcs_info")
    if isinstance(vcs, dict):
        commit = vcs.get("commit_id")
        if isinstance(commit, str) and _SHA_RE.fullmatch(commit.lower()):
            return commit.lower()
    url = document.get("url")
    if isinstance(url, str):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "file":
            checkout = Path(urllib.parse.unquote(parsed.path))
            source = checkout / "src" / "proof_check"
            if source.is_dir():
                return _git_commit(checkout, source)
    return None


def _configure_contract_schema_dir() -> None:
    """Locate the frozen root schemas for a source-checkout installation.

    N17 intentionally keeps the schemas at the repository root.  Setuptools
    records the checkout URL for a local installation, so the CLI can use
    those same bytes without copying or modifying the core package.
    """

    try:
        contract_module._schema_dir()
        return
    except ValueError:
        pass
    document = _direct_url_document()
    url = None if document is None else document.get("url")
    if not isinstance(url, str):
        return
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "file":
        return
    candidate = Path(urllib.parse.unquote(parsed.path)) / "schemas"
    required = (candidate / contract_module.RECEIPT_SCHEMA_FILE, candidate / contract_module.POLICY_SCHEMA_FILE)
    if not all(path.is_file() for path in required):
        return
    contract_module._schema_dir = lambda: candidate
    contract_module.load_schema.cache_clear()
    contract_module._validator.cache_clear()


def resolve_source_commit(explicit: str | None = None) -> str:
    """Return a recorded source revision or refuse to invent one."""

    if explicit is not None:
        value = explicit.lower()
        if not _SHA_RE.fullmatch(value):
            raise ValueError("source commit must be 40 lowercase hexadecimal characters")
        return value
    direct = _direct_url_commit()
    if direct is not None:
        return direct
    local = _git_commit(Path(__file__).resolve().parent)
    if local is not None:
        return local
    raise ValueError("installed source commit is unknown; install from a clean commit-pinned checkout")


def _write_receipt(path: Path, receipt: Receipt) -> None:
    parent = path.parent
    if not parent.is_dir():
        raise ValueError(f"receipt directory does not exist: {parent}")
    temporary = parent / f".{path.name}.proof-check-tmp"
    try:
        temporary.write_bytes(canonical_bytes(receipt.to_dict()))
        temporary.replace(path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"receipt cannot be written: {path}") from error


def _token(name: str) -> str:
    if not name or "=" in name or "\x00" in name:
        raise ValueError("token environment variable name is invalid")
    value = os.environ.get(name)
    if value is None or not value:
        raise ValueError(f"required token environment variable is missing: {name}")
    if "\r" in value or "\n" in value:
        raise ValueError(f"token environment variable must contain one line: {name}")
    return value


def _check(
    args: argparse.Namespace,
    *,
    transport: Transport | None,
    source_commit: str | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.target == "merge_group":
        print("REFUSED: merge_group target requires GitHub event context", file=stderr)
        return EXIT_INVALID
    if args.pr < 1:
        print("REFUSED: pull request number must be positive", file=stderr)
        return EXIT_INVALID
    receipt_path = Path(args.receipt)
    if not receipt_path.parent.is_dir():
        print(f"REFUSED: receipt directory does not exist: {receipt_path.parent}", file=stderr)
        return EXIT_INVALID
    try:
        _configure_contract_schema_dir()
        policy_data = _read_json_object(Path(args.policy), "policy")
        if policy_data.get("schema_version") == POLICY_SCHEMA_VERSION:
            validate_policy(policy_data)
            policy = PolicyDocument.from_dict(policy_data)
        else:
            # The deterministic core owns the closed-version response.  A
            # syntactically valid object with another version is observable
            # evidence and yields INDETERMINATE rather than a guessed policy.
            policy = None
        declaration_text = None if args.declaration is None else _read_text(Path(args.declaration), "declaration")
        token = _token(args.token_env)
        commit = resolve_source_commit(source_commit)
        if policy is None:
            # Collection still needs a bounded source decision.  An inert inline
            # policy is used only to gather coordinates; evaluate() receives the
            # original versioned object and returns the core's closed response.
            collector_policy = PolicyDocument.from_dict(
                {
                    "schema_version": POLICY_SCHEMA_VERSION,
                    "scope": {"source": "pr_body_allowlist", "trust": "none", "entries": ["README.md"]},
                    "checks": {"required_selectors": []},
                    "reviews": {"policy": "none"},
                    "verdict_policy": {"fail_exit": 10, "indeterminate_exit": 20},
                    "receipt": {"mode": "always"},
                }
            )
        else:
            collector_policy = policy
    except (ValueError, SchemaViolation) as error:
        print(f"REFUSED: {error}", file=stderr)
        return EXIT_INVALID

    observation_errors: list[str] = []
    try:
        evidence = collect_evidence(
            args.repo,
            args.pr,
            collector_policy,
            token,
            target=args.target,
            declaration_text=declaration_text,
            transport=transport,
            error_reporter=observation_errors.append,
        )
        evaluation = evaluate(policy_data if policy is None else policy, evidence)
        receipt = build_receipt(evaluation, evidence, commit)
        _write_receipt(receipt_path, receipt)
    except ObservationUnavailable:
        print("INDETERMINATE: EVIDENCE_MISSING: pull request coordinates could not be observed", file=stdout)
        return EXIT_INDETERMINATE
    except (ConfigurationError, SchemaViolation, ValueError) as error:
        print(f"REFUSED: {error}", file=stderr)
        return EXIT_INVALID
    except Exception:
        # Stable output keeps transport response text and credentials out of
        # the log even when an injected adapter fails unexpectedly.
        print("INDETERMINATE: EVIDENCE_MISSING: GitHub observation failed", file=stdout)
        return EXIT_INDETERMINATE

    print(f"{evaluation.verdict.value}: {evaluation.explanation}", file=stdout)
    if evaluation.verdict is Verdict.INDETERMINATE and observation_errors:
        print("OBSERVATION_ERRORS: " + ", ".join(dict.fromkeys(observation_errors)), file=stdout)
    if evaluation.verdict is Verdict.PASS:
        return EXIT_PASS
    if evaluation.verdict is Verdict.FAIL:
        return EXIT_FAIL
    if policy is not None and policy.verdict_policy.indeterminate_exit == 0:
        print("EXIT 0 BY POLICY OPT-OUT (verdict_policy.indeterminate_exit: 0)", file=stdout)
        return EXIT_PASS
    return EXIT_INDETERMINATE


def _verify_bundle(receipt_bytes: bytes, bundle: Path) -> tuple[Any, list[str]]:
    if not bundle.is_dir():
        raise ValueError(f"offline bundle is not a directory: {bundle}")
    policy = _read_json_object(bundle / "policy.json", "bundle policy")
    if policy.get("schema_version") == POLICY_SCHEMA_VERSION:
        validate_policy(policy)
    result = verify_receipt(receipt_bytes, policy=policy)
    findings = list(result.findings)
    declaration_path = bundle / "declaration.txt"
    declaration_text: str | None = None
    if declaration_path.exists() and result.receipt is not None:
        try:
            declaration = declaration_path.read_bytes()
            declaration_text = declaration.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError(f"bundle declaration is unreadable UTF-8: {declaration_path}") from error
        recorded = result.receipt.policy.raw_sha256
        actual = sha256_hex(declaration)
        if recorded is None or actual != recorded:
            findings.append("declaration.txt digest does not match the receipt policy record")
    if result.receipt is not None:
        source_type = result.receipt.policy.source_type
        expected_entries: tuple[str, ...] | None = None
        scope = policy.get("scope")
        inline_entries = scope.get("entries") if isinstance(scope, dict) else None
        try:
            if source_type is SourceType.NONE:
                expected_entries = ()
            elif source_type is SourceType.AUTHOR_PR_BODY:
                if declaration_text is not None:
                    expected_entries = parse_pr_body_allowlist(declaration_text)
            elif source_type is SourceType.OWNER_PINNED_INPUT and isinstance(inline_entries, list):
                expected_entries = tuple(inline_entries)
            elif source_type in (SourceType.OWNER_PINNED_INPUT, SourceType.TRUSTED_BASE_MANIFEST):
                if declaration_text is not None:
                    expected_entries = parse_manifest(declaration_text)
        except ScopeRefusal as error:
            findings.append(f"bundle declaration is refused ({error.code})")
        if expected_entries is None:
            findings.append("bundle does not contain the declaration needed to derive allowed_entries")
        elif expected_entries != result.receipt.policy.allowed_entries:
            findings.append("bundle declaration allowed_entries do not match the receipt policy record")
    return result, findings


def _online_differences(receipt: Receipt, current: Any) -> list[str]:
    lines: list[str] = []
    unavailable = tuple(error for error in current.errors if error != "COORDINATE_DRIFT")
    if unavailable:
        lines.append("REOBSERVATION_UNAVAILABLE: " + ", ".join(unavailable))
    if "COORDINATE_DRIFT" in current.errors:
        lines.append("COORDINATE_DRIFT: the pull request head changed during online re-observation")
    if current.head_sha is not None and current.head_sha != receipt.subject.head_sha:
        lines.append(f"HEAD_MOVED: recorded={receipt.subject.head_sha} current={current.head_sha}")
    recorded_count = receipt.observations.changed_paths.api_reported_count
    if current.changed_files is not None and current.changed_files != recorded_count:
        lines.append(f"FILE_COUNT_CHANGED: recorded={recorded_count} current={current.changed_files}")
    recorded_checks = tuple(sorted(receipt.observations.checks, key=lambda item: item.id))
    if current.checks != recorded_checks:
        lines.append(f"CHECKS_CHANGED: recorded={len(recorded_checks)} current={len(current.checks)}")
    if not lines:
        lines.append("NO_REOBSERVED_DIFFERENCES")
    return lines


def _verify(
    args: argparse.Namespace,
    *,
    transport: Transport | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    receipt_path = Path(args.receipt)
    try:
        receipt_bytes = receipt_path.read_bytes()
    except OSError:
        print(f"REFUSED: receipt is unreadable: {receipt_path}", file=stderr)
        return EXIT_INVALID
    try:
        _configure_contract_schema_dir()
        if args.offline_bundle is not None:
            result, findings = _verify_bundle(receipt_bytes, Path(args.offline_bundle))
        else:
            result = verify_receipt(receipt_bytes)
            findings = list(result.findings)
    except (ValueError, SchemaViolation) as error:
        print(f"REFUSED: {error}", file=stderr)
        return EXIT_INVALID

    malformed = result.receipt is None and any(
        finding.startswith("malformed receipt:") or finding.startswith("schema:") for finding in findings
    )
    if malformed:
        for finding in findings:
            print(f"MALFORMED: {finding}", file=stderr)
        return EXIT_INVALID

    if args.offline_bundle is None and result.receipt is not None:
        try:
            token = _token(args.token_env)
        except ValueError as error:
            print(f"REFUSED: {error}", file=stderr)
            return EXIT_INVALID
        current = reobserve_pull_request(
            result.receipt.subject.repository,
            result.receipt.subject.pr_number,
            token,
            transport=transport,
        )
        print("ONLINE_REOBSERVATION (recorded verdict unchanged):", file=stdout)
        for line in _online_differences(result.receipt, current):
            print(f"- {line}", file=stdout)

    if findings:
        for finding in findings:
            print(f"UNVERIFIABLE: {finding}", file=stdout)
        return EXIT_INDETERMINATE
    print("VERIFIED: canonical consistency, schema closure, and recorded digests", file=stdout)
    return EXIT_PASS


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    source_commit: str | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the CLI and return one of 0, 10, 20, or 2."""

    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        args = _parser().parse_args(argv)
    except _UsageError as error:
        print(f"REFUSED: invalid usage: {error}", file=err)
        return EXIT_INVALID
    if args.command == "check":
        return _check(args, transport=transport, source_commit=source_commit, stdout=out, stderr=err)
    return _verify(args, transport=transport, stdout=out, stderr=err)
