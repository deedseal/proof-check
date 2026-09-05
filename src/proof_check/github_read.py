"""Bounded, GET-only GitHub observation for one pull request.

The deterministic core deliberately has no network access.  This module is
the adapter that turns GitHub REST responses into its ``Evidence`` input.  A
transport is injectable for replay tests; every call still passes through the
same method, host, and repository-prefix guard.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from proof_check.contracts import (
    ChangedPathRecord,
    CheckObservation,
    Observation,
    PolicyDocument,
    ReviewObservation,
    Subject,
    TargetKind,
)
from proof_check.verify import CHANGED_FILES_CEILING, ChangedPathsObservation, Declaration, Evidence

__all__ = [
    "API_HOST",
    "GitHubAPIError",
    "GitHubReader",
    "HTTPResponse",
    "ObservationUnavailable",
    "OnlineObservation",
    "Transport",
    "TransportRefusal",
    "UrllibTransport",
    "collect_evidence",
    "reobserve_pull_request",
]

API_HOST = "api.github.com"
API_ROOT = f"https://{API_HOST}"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_API_VERSION = "2022-11-28"
_PAGE_SIZE = 100
_PAGE_CEILING = CHANGED_FILES_CEILING // _PAGE_SIZE


class TransportRefusal(ValueError):
    """A request falls outside the read-only GitHub boundary."""


class GitHubAPIError(RuntimeError):
    """A bounded GitHub read failed or returned an unusable document."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ObservationUnavailable(GitHubAPIError):
    """The PR identity could not be read, so a schema-valid receipt cannot be built."""


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    def request(self, method: str, url: str, headers: Mapping[str, str]) -> HTTPResponse:
        """Return one HTTP response without interpreting its JSON body."""


class UrllibTransport:
    """Small stdlib HTTPS transport with a fixed GitHub repository boundary."""

    def __init__(self, repository: str, *, timeout: float = 30.0):
        self._prefix = _repository_api_prefix(repository)
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_RefuseRedirect)

    def request(self, method: str, url: str, headers: Mapping[str, str]) -> HTTPResponse:
        _guard_request(method, url, self._prefix)
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return HTTPResponse(int(response.status), dict(response.headers.items()), response.read())
        except urllib.error.HTTPError as error:
            # Response bytes are intentionally discarded.  They are not needed
            # for evaluation and may contain repository-owned text.
            return HTTPResponse(int(error.code), dict(error.headers.items()) if error.headers else {}, b"")
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise GitHubAPIError("GITHUB_UNREACHABLE") from error


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Keep every network hop on the URL that passed the boundary guard."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


@dataclass(frozen=True, slots=True)
class OnlineObservation:
    """The fields online verification compares with a recorded receipt."""

    head_sha: str | None
    changed_files: int | None
    checks: tuple[CheckObservation, ...]
    errors: tuple[str, ...] = ()


def _validate_repository(repository: str) -> None:
    if not _REPOSITORY_RE.fullmatch(repository) or repository.split("/", 1)[1] in {".", ".."}:
        raise ValueError("repository must be owner/name using GitHub repository characters")


def _repository_api_prefix(repository: str) -> str:
    _validate_repository(repository)
    owner, name = repository.split("/", 1)
    return f"/repos/{urllib.parse.quote(owner, '')}/{urllib.parse.quote(name, '')}/"


def _guard_request(method: str, url: str, repository_prefix: str) -> None:
    if method != "GET":
        raise TransportRefusal("GITHUB_METHOD_REFUSED: only GET is permitted")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != API_HOST:
        raise TransportRefusal(f"GITHUB_HOST_REFUSED: expected {API_HOST}")
    if not parsed.path.startswith(repository_prefix):
        raise TransportRefusal("GITHUB_REPOSITORY_BOUNDARY_REFUSED")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise TransportRefusal("GITHUB_URL_REFUSED")


def _coerce_response(value: Any) -> HTTPResponse:
    if isinstance(value, HTTPResponse):
        return value
    if isinstance(value, tuple) and len(value) == 3:
        status, headers, body = value
        if isinstance(body, str):
            body = body.encode("utf-8")
        return HTTPResponse(int(status), dict(headers), bytes(body))
    raise GitHubAPIError("GITHUB_TRANSPORT_RESPONSE_INVALID")


class GitHubReader:
    """Repository-bound GitHub REST reader.

    The token is used only to construct the Authorization header.  Errors are
    reported by stable names so response or credential bytes never enter
    receipts, stdout, stderr, or execution logs.
    """

    def __init__(self, repository: str, token: str, transport: Transport | Callable[..., Any] | None = None):
        _validate_repository(repository)
        if not token or "\r" in token or "\n" in token:
            raise ValueError("token must be a non-empty single-line value")
        self.repository = repository
        self._prefix = _repository_api_prefix(repository)
        self._token = token
        self._transport = transport if transport is not None else UrllibTransport(repository)
        self.last_response_headers: Mapping[str, str] = {}

    def get_json(self, path: str, query: Mapping[str, str | int] | None = None) -> Any:
        if path.startswith("/") or ".." in path.split("/"):
            raise TransportRefusal("GITHUB_PATH_REFUSED")
        url = f"{API_ROOT}{self._prefix}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        _guard_request("GET", url, self._prefix)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "proof-check/0.0.0",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        try:
            if callable(self._transport) and not hasattr(self._transport, "request"):
                response = _coerce_response(self._transport("GET", url, headers))
            else:
                response = _coerce_response(self._transport.request("GET", url, headers))  # type: ignore[union-attr]
        except (TransportRefusal, GitHubAPIError):
            raise
        except Exception as error:
            raise GitHubAPIError("GITHUB_TRANSPORT_FAILED") from error
        if response.status < 200 or response.status >= 300:
            if response.status == 403:
                raise GitHubAPIError("GITHUB_RATE_LIMIT_OR_FORBIDDEN")
            raise GitHubAPIError(f"GITHUB_HTTP_{response.status}")
        self.last_response_headers = response.headers
        try:
            return json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubAPIError("GITHUB_JSON_INVALID") from error


def _object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubAPIError(code)
    return value


def _array(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise GitHubAPIError(code)
    return value


def _string(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise GitHubAPIError(code)
    return value


def _integer(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GitHubAPIError(code)
    return value


def _sha(value: Any, code: str) -> str:
    text = _string(value, code).lower()
    if not _SHA_RE.fullmatch(text):
        raise GitHubAPIError(code)
    return text


def _optional_sha(value: Any, code: str) -> str | None:
    return None if value is None else _sha(value, code)


def _utc_timestamp(value: Any, code: str) -> str:
    text = _string(value, code)
    if not _UTC_RE.fullmatch(text):
        raise GitHubAPIError(code)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise GitHubAPIError(code) from error
    return text


def _utc_now(clock: Callable[[], datetime] | None) -> str:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _has_next_page(headers: Mapping[str, str]) -> bool:
    link = next((value for name, value in headers.items() if name.lower() == "link"), "")
    return any('rel="next"' in part for part in link.split(","))


def _has_link_header(headers: Mapping[str, str]) -> bool:
    return any(name.lower() == "link" for name in headers)


def _pull_coordinates(document: Any, repository: str, pr_number: int, target: TargetKind) -> tuple[Subject, str | None, str, bool]:
    pull = _object(document, "PULL_REQUEST_RESPONSE_INVALID")
    base = _object(pull.get("base"), "PULL_REQUEST_BASE_MISSING")
    head = _object(pull.get("head"), "PULL_REQUEST_HEAD_MISSING")
    base_repo = _object(base.get("repo"), "PULL_REQUEST_REPOSITORY_MISSING")
    head_sha = _sha(head.get("sha"), "PULL_REQUEST_HEAD_SHA_MISSING")
    merge_sha = _optional_sha(pull.get("merge_commit_sha"), "PULL_REQUEST_MERGE_SHA_INVALID")
    evaluated_sha = head_sha if target is TargetKind.HEAD or merge_sha is None else merge_sha
    author = pull.get("user")
    author_login = None
    if isinstance(author, dict) and author.get("login") is not None:
        author_login = _string(author.get("login"), "PULL_REQUEST_AUTHOR_INVALID")
    state = _string(pull.get("state"), "PULL_REQUEST_STATE_MISSING")
    draft = pull.get("draft")
    if not isinstance(draft, bool):
        raise GitHubAPIError("PULL_REQUEST_DRAFT_MISSING")
    private = base_repo.get("private")
    if not isinstance(private, bool):
        raise GitHubAPIError("PULL_REQUEST_REPOSITORY_VISIBILITY_MISSING")
    subject = Subject(
        repository=repository,
        repository_id=_integer(base_repo.get("id"), "PULL_REQUEST_REPOSITORY_ID_MISSING"),
        repository_visibility="private" if private else "public",
        pr_number=pr_number,
        target_kind=target,
        evaluated_sha=evaluated_sha,
        base_ref=_string(base.get("ref"), "PULL_REQUEST_BASE_REF_MISSING"),
        base_sha=_sha(base.get("sha"), "PULL_REQUEST_BASE_SHA_MISSING"),
        head_ref=_string(head.get("ref"), "PULL_REQUEST_HEAD_REF_MISSING"),
        head_sha=head_sha,
        merge_or_group_sha=merge_sha,
    )
    return subject, author_login, state, draft


def _collect_files(reader: GitHubReader, pr_number: int, expected_count: int) -> tuple[ChangedPathsObservation, tuple[str, ...]]:
    records: list[ChangedPathRecord] = []
    names: set[str] = set()
    errors: list[str] = []
    page = 1
    truncated = False
    while len(records) < CHANGED_FILES_CEILING:
        try:
            items = _array(
                reader.get_json(f"pulls/{pr_number}/files", {"per_page": _PAGE_SIZE, "page": page}),
                "PULL_REQUEST_FILES_RESPONSE_INVALID",
            )
            for item_value in items:
                item = _object(item_value, "PULL_REQUEST_FILE_INVALID")
                previous = item.get("previous_filename")
                filename = _string(item.get("filename"), "PULL_REQUEST_FILE_NAME_MISSING")
                if filename in names:
                    raise GitHubAPIError("PULL_REQUEST_FILE_DUPLICATED")
                names.add(filename)
                records.append(
                    ChangedPathRecord(
                        filename=filename,
                        status=_string(item.get("status"), "PULL_REQUEST_FILE_STATUS_MISSING"),
                        previous_filename=None if previous is None else _string(previous, "PULL_REQUEST_PREVIOUS_FILE_NAME_INVALID"),
                    )
                )
                if len(records) >= CHANGED_FILES_CEILING:
                    break
        except (GitHubAPIError, TransportRefusal) as error:
            errors.append(getattr(error, "code", "GITHUB_REQUEST_REFUSED"))
            truncated = True
            break
        if len(records) >= CHANGED_FILES_CEILING:
            truncated = True
            break
        if len(items) < _PAGE_SIZE:
            break
        page += 1
    if len(records) != expected_count:
        truncated = True
    return ChangedPathsObservation(expected_count, tuple(records), truncated), tuple(errors)


def _collect_checks(reader: GitHubReader, sha: str) -> tuple[tuple[CheckObservation, ...], tuple[str, ...]]:
    records: list[CheckObservation] = []
    identifiers: set[int] = set()
    errors: list[str] = []
    page = 1
    total: int | None = None
    while page <= _PAGE_CEILING:
        try:
            document = _object(
                reader.get_json(f"commits/{sha}/check-runs", {"per_page": _PAGE_SIZE, "page": page}),
                "CHECK_RUNS_RESPONSE_INVALID",
            )
            reported = _integer(document.get("total_count"), "CHECK_RUNS_COUNT_MISSING")
            total = reported if total is None else total
            if reported != total:
                raise GitHubAPIError("CHECK_RUNS_COUNT_CHANGED")
            items = _array(document.get("check_runs"), "CHECK_RUNS_LIST_MISSING")
            for item_value in items:
                item = _object(item_value, "CHECK_RUN_INVALID")
                suite = _object(item.get("check_suite"), "CHECK_SUITE_MISSING")
                conclusion = item.get("conclusion")
                completed = item.get("completed_at")
                identifier = _integer(item.get("id"), "CHECK_RUN_ID_MISSING")
                if identifier in identifiers:
                    raise GitHubAPIError("CHECK_RUN_DUPLICATED")
                identifiers.add(identifier)
                records.append(
                    CheckObservation(
                        id=identifier,
                        suite_id=_integer(suite.get("id"), "CHECK_SUITE_ID_MISSING"),
                        name=_string(item.get("name"), "CHECK_RUN_NAME_MISSING"),
                        head_sha=_sha(item.get("head_sha"), "CHECK_RUN_HEAD_SHA_MISSING"),
                        status=_string(item.get("status"), "CHECK_RUN_STATUS_MISSING"),
                        conclusion=None if conclusion is None else _string(conclusion, "CHECK_RUN_CONCLUSION_INVALID"),
                        completed_at=None if completed is None else _utc_timestamp(completed, "CHECK_RUN_COMPLETED_AT_INVALID"),
                    )
                )
        except (GitHubAPIError, TransportRefusal) as error:
            errors.append(getattr(error, "code", "GITHUB_REQUEST_REFUSED"))
            break
        if len(items) < _PAGE_SIZE:
            break
        if total is not None and len(records) >= total:
            break
        if page == _PAGE_CEILING:
            errors.append("CHECK_RUNS_PAGINATION_LIMIT")
            break
        page += 1
    if total is not None and len(records) != total:
        errors.append("CHECK_RUNS_COUNT_MISMATCH")
    return tuple(records), tuple(dict.fromkeys(errors))


def _collect_reviews(reader: GitHubReader, pr_number: int) -> tuple[tuple[ReviewObservation, ...], tuple[str, ...]]:
    records: list[ReviewObservation] = []
    identifiers: set[int] = set()
    errors: list[str] = []
    page = 1
    while page <= _PAGE_CEILING:
        try:
            items = _array(
                reader.get_json(f"pulls/{pr_number}/reviews", {"per_page": _PAGE_SIZE, "page": page}),
                "REVIEWS_RESPONSE_INVALID",
            )
            has_link_header = _has_link_header(reader.last_response_headers)
            has_next_page = _has_next_page(reader.last_response_headers)
            for item_value in items:
                item = _object(item_value, "REVIEW_INVALID")
                user = _object(item.get("user"), "REVIEW_USER_MISSING")
                identifier = _integer(item.get("id"), "REVIEW_ID_MISSING")
                if identifier in identifiers:
                    raise GitHubAPIError("REVIEW_DUPLICATED")
                identifiers.add(identifier)
                records.append(
                    ReviewObservation(
                        id=identifier,
                        actor=_string(user.get("login"), "REVIEW_ACTOR_MISSING"),
                        association=_string(item.get("author_association"), "REVIEW_ASSOCIATION_MISSING"),
                        state=_string(item.get("state"), "REVIEW_STATE_MISSING"),
                        submitted_at=_utc_timestamp(item.get("submitted_at"), "REVIEW_SUBMITTED_AT_MISSING"),
                        commit_id=_sha(item.get("commit_id"), "REVIEW_COMMIT_ID_MISSING"),
                    )
                )
        except (GitHubAPIError, TransportRefusal) as error:
            errors.append(getattr(error, "code", "GITHUB_REQUEST_REFUSED"))
            break
        if len(items) < _PAGE_SIZE:
            break
        if page == _PAGE_CEILING:
            if not has_link_header or has_next_page:
                errors.append("REVIEWS_PAGINATION_LIMIT")
            break
        page += 1
    return tuple(records), tuple(errors)


def _declaration(
    reader: GitHubReader,
    pull: Mapping[str, Any],
    policy: PolicyDocument,
    subject: Subject,
    pinned_text: str | None,
) -> tuple[Declaration, tuple[str, ...]]:
    scope = policy.scope
    if scope.entries is not None:
        return Declaration(), ()
    kind = scope.source_kind()
    if kind == "pr_body_allowlist":
        body = pull.get("body")
        if body is not None and not isinstance(body, str):
            return Declaration(None, None, f"https://github.com/{subject.repository}/pull/{subject.pr_number}"), (
                "PULL_REQUEST_BODY_INVALID",
            )
        return Declaration(body, None, f"https://github.com/{subject.repository}/pull/{subject.pr_number}"), ()
    if kind == "pinned":
        return Declaration(pinned_text, None, None), ()
    if kind != "file":
        return Declaration(), ()
    manifest_path = scope.source_argument()
    if not manifest_path or manifest_path.startswith("/") or ".." in manifest_path.split("/"):
        return Declaration(None, subject.base_sha, None), ("MANIFEST_PATH_REFUSED",)
    encoded = urllib.parse.quote(manifest_path, "/")
    locator = f"https://github.com/{subject.repository}/blob/{subject.base_sha}/{encoded}"
    try:
        document = _object(
            reader.get_json(f"contents/{encoded}", {"ref": subject.base_sha}),
            "MANIFEST_RESPONSE_INVALID",
        )
        if document.get("encoding") != "base64":
            raise GitHubAPIError("MANIFEST_ENCODING_UNSUPPORTED")
        content = _string(document.get("content"), "MANIFEST_CONTENT_MISSING")
        raw = base64.b64decode(content, validate=False)
        text = raw.decode("utf-8")
        return Declaration(text, subject.base_sha, locator), ()
    except (ValueError, UnicodeDecodeError, GitHubAPIError, TransportRefusal):
        return Declaration(None, subject.base_sha, locator), ("MANIFEST_READ_FAILED",)


def collect_evidence(
    repository: str,
    pr_number: int,
    policy: PolicyDocument,
    token: str,
    *,
    target: str = "head",
    declaration_text: str | None = None,
    transport: Transport | Callable[..., Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    error_reporter: Callable[[str], None] | None = None,
) -> Evidence:
    """Collect one bounded observation and return deterministic-core input.

    Once the PR coordinates are known, any missing page or malformed secondary
    response marks the changed-path observation incomplete.  The core then
    emits ``INDETERMINATE`` / ``EVIDENCE_MISSING``; a partial observation can
    never become ``PASS``.
    """

    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        raise ValueError("pull request number must be a positive integer")
    try:
        target_kind = TargetKind(target)
    except ValueError as error:
        raise ValueError("target must be head or test_merge") from error
    if target_kind is TargetKind.MERGE_GROUP:
        raise ValueError("merge_group is available only from GitHub event context")

    reader = GitHubReader(repository, token, transport)
    try:
        pull = _object(reader.get_json(f"pulls/{pr_number}"), "PULL_REQUEST_RESPONSE_INVALID")
        subject, author, _state, _draft = _pull_coordinates(pull, repository, pr_number, target_kind)
        expected_count = _integer(pull.get("changed_files"), "PULL_REQUEST_CHANGED_FILES_MISSING")
    except (GitHubAPIError, TransportRefusal) as error:
        raise ObservationUnavailable("PULL_REQUEST_COORDINATES_UNAVAILABLE") from error

    changed, file_errors = _collect_files(reader, pr_number, expected_count)
    checks, check_errors = _collect_checks(reader, subject.head_sha)
    reviews, review_errors = _collect_reviews(reader, pr_number)
    declaration, declaration_errors = _declaration(reader, pull, policy, subject, declaration_text)
    collected_errors = list(file_errors + check_errors + review_errors + declaration_errors)

    observed_head: str | None
    try:
        final_pull = _object(reader.get_json(f"pulls/{pr_number}"), "PULL_REQUEST_RESPONSE_INVALID")
        final_head = _object(final_pull.get("head"), "PULL_REQUEST_HEAD_MISSING")
        observed_head = _sha(final_head.get("sha"), "PULL_REQUEST_HEAD_SHA_MISSING")
        final_count = _integer(final_pull.get("changed_files"), "PULL_REQUEST_CHANGED_FILES_MISSING")
        if final_count != expected_count:
            changed = ChangedPathsObservation(changed.api_reported_count, changed.records, True)
            collected_errors.append("PULL_REQUEST_CHANGED_FILES_CHANGED")
    except (GitHubAPIError, TransportRefusal):
        observed_head = None
        collected_errors.append("PULL_REQUEST_HEAD_READBACK_FAILED")

    if target_kind is TargetKind.TEST_MERGE and subject.merge_or_group_sha is None:
        observed_head = None
        collected_errors.append("TEST_MERGE_SHA_MISSING")
    # The core already represents a missing declaration and a failed head
    # readback directly.  Check and review pagination have no completeness
    # field in rules/v0, so those failures make the overall changed-path
    # observation incomplete to prevent a partial bundle from passing.
    if (check_errors or review_errors) and not changed.truncated:
        changed = ChangedPathsObservation(changed.api_reported_count, changed.records, True)
    if error_reporter is not None:
        for code in dict.fromkeys(collected_errors):
            error_reporter(code)
    captured = _utc_now(clock)
    observation = Observation(
        cutoff_utc=captured,
        captured_at_utc=captured,
        watermark=f"github-rest:{repository}:pull:{pr_number}:{subject.head_sha}:{captured}",
    )
    return Evidence(
        subject=subject,
        observation=observation,
        changed_paths=changed,
        declaration=declaration,
        observed_head_sha=observed_head,
        checks=checks,
        reviews=reviews,
        pr_author=author,
    )


def reobserve_pull_request(
    repository: str,
    pr_number: int,
    token: str,
    *,
    transport: Transport | Callable[..., Any] | None = None,
) -> OnlineObservation:
    """Read current head, file count, and checks for online comparison."""

    reader = GitHubReader(repository, token, transport)
    errors: list[str] = []
    try:
        pull = _object(reader.get_json(f"pulls/{pr_number}"), "PULL_REQUEST_RESPONSE_INVALID")
        head = _object(pull.get("head"), "PULL_REQUEST_HEAD_MISSING")
        head_sha = _sha(head.get("sha"), "PULL_REQUEST_HEAD_SHA_MISSING")
        changed_files = _integer(pull.get("changed_files"), "PULL_REQUEST_CHANGED_FILES_MISSING")
    except (GitHubAPIError, TransportRefusal):
        return OnlineObservation(None, None, (), ("PULL_REQUEST_COORDINATES_UNAVAILABLE",))
    checks, check_errors = _collect_checks(reader, head_sha)
    errors.extend(check_errors)
    try:
        final_pull = _object(reader.get_json(f"pulls/{pr_number}"), "PULL_REQUEST_RESPONSE_INVALID")
        final_head = _object(final_pull.get("head"), "PULL_REQUEST_HEAD_MISSING")
        final_sha = _sha(final_head.get("sha"), "PULL_REQUEST_HEAD_SHA_MISSING")
        if final_sha != head_sha:
            errors.append("COORDINATE_DRIFT")
            head_sha = final_sha
    except (GitHubAPIError, TransportRefusal):
        errors.append("PULL_REQUEST_HEAD_READBACK_FAILED")
    return OnlineObservation(head_sha, changed_files, tuple(sorted(checks, key=lambda item: item.id)), tuple(dict.fromkeys(errors)))
