"""GET boundary, pagination, and incomplete-observation behavior."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from proof_check.contracts import DetailCode, PolicyDocument, ReasonCode, Verdict
from proof_check.github_read import (
    HTTPResponse,
    GitHubReader,
    TransportRefusal,
    UrllibTransport,
    _RefuseRedirect,
    collect_evidence,
)
from proof_check.verify import evaluate

FIXTURES = Path(__file__).parent / "fixtures" / "github_read"
HEAD = "1" * 40
MOVED = "4" * 40


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _policy(source: str = "pr_body_allowlist") -> PolicyDocument:
    return PolicyDocument.from_dict(
        {
            "schema_version": "proof-check-policy/v1",
            "scope": {"source": source, "trust": "advisory" if source == "pr_body_allowlist" else "blocking"},
            "checks": {"required_selectors": []},
            "reviews": {"policy": "report_only"},
            "verdict_policy": {"fail_exit": 10, "indeterminate_exit": 20},
            "receipt": {"mode": "always"},
        }
    )


class FixtureTransport:
    def __init__(
        self,
        *,
        reported_count: int = 250,
        readback_count: int | None = None,
        moved: bool = False,
        check_status: int = 200,
        review_status: int = 200,
        file_pages: dict[int, list[dict]] | None = None,
        check_pages: dict[int, dict] | None = None,
        review_pages: dict[int, list[dict]] | None = None,
    ):
        self.reported_count = reported_count
        self.readback_count = readback_count
        self.moved = moved
        self.check_status = check_status
        self.review_status = review_status
        self.pull_reads = 0
        self.calls: list[tuple[str, str]] = []
        self.file_pages = file_pages or {1: _load("files-page-1.json"), 2: _load("files-page-2.json"), 3: _load("files-page-3.json")}
        self.check_pages = check_pages or {1: _load("check-runs-page-1.json")}
        self.review_pages = review_pages or {1: _load("reviews-page-1.json")}

    @staticmethod
    def response(value, status: int = 200) -> HTTPResponse:
        return HTTPResponse(status, {}, json.dumps(value).encode("utf-8"))

    def request(self, method: str, url: str, headers) -> HTTPResponse:
        self.calls.append((method, url))
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        page = int(query.get("page", ["1"])[0])
        if parsed.path.endswith("/pulls/42"):
            self.pull_reads += 1
            pull = _load("pull.json")
            pull["changed_files"] = (
                self.readback_count
                if self.readback_count is not None and self.pull_reads > 1
                else self.reported_count
            )
            if self.moved and self.pull_reads > 1:
                pull["head"]["sha"] = MOVED
            return self.response(pull)
        if parsed.path.endswith("/pulls/42/files"):
            return self.response(self.file_pages.get(page, []))
        if parsed.path.endswith(f"/commits/{HEAD}/check-runs"):
            if self.check_status != 200:
                return self.response({}, self.check_status)
            return self.response(self.check_pages.get(page, {"total_count": 0, "check_runs": []}))
        if parsed.path.endswith("/pulls/42/reviews"):
            if self.review_status != 200:
                return self.response({}, self.review_status)
            return self.response(self.review_pages.get(page, []))
        return self.response({}, 404)


def _clock() -> datetime:
    return datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def test_250_file_fixture_uses_three_pages_and_is_complete():
    transport = FixtureTransport()
    evidence = collect_evidence(
        "example-org/example-repo", 42, _policy(), "token-value", transport=transport, clock=_clock
    )
    assert len(evidence.changed_paths.records) == 250
    assert evidence.changed_paths.truncated is False
    assert evaluate(_policy(), evidence).verdict is Verdict.PASS
    file_urls = [url for _method, url in transport.calls if "/files?" in url]
    assert [parse_qs(urlsplit(url).query)["page"] for url in file_urls] == [["1"], ["2"], ["3"]]
    assert all(method == "GET" for method, _url in transport.calls)
    assert all(urlsplit(url).netloc == "api.github.com" for _method, url in transport.calls)
    assert all(urlsplit(url).path.startswith("/repos/example-org/example-repo/") for _method, url in transport.calls)


def test_file_pagination_reads_until_a_short_page_before_comparing_count():
    pages = {
        1: [{"filename": f"docs/page-one-{value:03d}.md", "status": "modified"} for value in range(100)],
        2: [{"filename": f"docs/page-two-{value:03d}.md", "status": "modified"} for value in range(50)],
    }
    transport = FixtureTransport(reported_count=100, file_pages=pages)
    evidence = collect_evidence("example-org/example-repo", 42, _policy(), "token-value", transport=transport, clock=_clock)
    file_urls = [url for _method, url in transport.calls if "/files?" in url]
    assert [parse_qs(urlsplit(url).query)["page"] for url in file_urls] == [["1"], ["2"]]
    assert len(evidence.changed_paths.records) == 150
    assert evidence.changed_paths.truncated is True
    assert evaluate(_policy(), evidence).verdict is Verdict.INDETERMINATE


@pytest.mark.parametrize(
    "transport",
    [
        FixtureTransport(reported_count=251),
        FixtureTransport(reported_count=250, check_status=403),
    ],
)
def test_count_mismatch_or_api_failure_cannot_pass(transport):
    evidence = collect_evidence("example-org/example-repo", 42, _policy(), "token-value", transport=transport, clock=_clock)
    result = evaluate(_policy(), evidence)
    assert result.verdict is Verdict.INDETERMINATE
    assert ReasonCode.EVIDENCE_MISSING in result.reason_codes
    assert DetailCode.CHANGED_PATH_SET_INCOMPLETE in result.detail_codes


def test_repeated_file_record_cannot_pass():
    page = _load("files-page-1.json")
    transport = FixtureTransport(reported_count=2, file_pages={1: [page[0], page[0]]})
    evidence = collect_evidence("example-org/example-repo", 42, _policy(), "token-value", transport=transport, clock=_clock)
    result = evaluate(_policy(), evidence)
    assert result.verdict is Verdict.INDETERMINATE
    assert result.reason_codes == (ReasonCode.EVIDENCE_MISSING,)
    assert result.detail_codes == (DetailCode.CHANGED_PATH_SET_INCOMPLETE,)


def test_3000_file_ceiling_is_marked_incomplete():
    pages = {
        page: [
            {"filename": f"docs/ceiling-{(page - 1) * 100 + offset:04d}.md", "status": "modified"}
            for offset in range(100)
        ]
        for page in range(1, 31)
    }
    transport = FixtureTransport(reported_count=3000, file_pages=pages)
    evidence = collect_evidence("example-org/example-repo", 42, _policy(), "token-value", transport=transport, clock=_clock)
    result = evaluate(_policy(), evidence)
    assert len(evidence.changed_paths.records) == 3000
    assert evidence.changed_paths.truncated is True
    assert result.verdict is Verdict.INDETERMINATE
    assert result.reason_codes == (ReasonCode.EVIDENCE_MISSING,)
    assert result.detail_codes == (DetailCode.CHANGED_PATH_SET_INCOMPLETE,)


def test_head_readback_detects_coordinate_drift():
    transport = FixtureTransport(moved=True)
    evidence = collect_evidence("example-org/example-repo", 42, _policy(), "token-value", transport=transport, clock=_clock)
    result = evaluate(_policy(), evidence)
    assert evidence.subject.head_sha == HEAD
    assert evidence.observed_head_sha == MOVED
    assert result.verdict is Verdict.INDETERMINATE
    assert ReasonCode.STALE_HEAD in result.reason_codes
    assert DetailCode.COORDINATE_DRIFT in result.detail_codes


def test_changed_file_count_is_rechecked_on_the_second_pull_read():
    transport = FixtureTransport(readback_count=251)
    evidence = collect_evidence("example-org/example-repo", 42, _policy(), "token-value", transport=transport, clock=_clock)
    assert transport.pull_reads == 2
    assert evidence.changed_paths.truncated is True
    assert evaluate(_policy(), evidence).verdict is Verdict.INDETERMINATE


def test_checks_and_reviews_paginate_to_their_end():
    check_item = _load("check-runs-page-1.json")["check_runs"][0]
    check_pages = {
        1: {"total_count": 150, "check_runs": [{**check_item, "id": value} for value in range(1, 101)]},
        2: {"total_count": 150, "check_runs": [{**check_item, "id": value} for value in range(101, 151)]},
    }
    review_item = _load("reviews-page-1.json")[0]
    review_pages = {
        1: [{**review_item, "id": value} for value in range(1, 101)],
        2: [{**review_item, "id": value} for value in range(101, 151)],
    }
    transport = FixtureTransport(check_pages=check_pages, review_pages=review_pages)
    evidence = collect_evidence("example-org/example-repo", 42, _policy(), "token-value", transport=transport, clock=_clock)
    assert len(evidence.checks) == 150
    assert len(evidence.reviews) == 150
    assert evaluate(_policy(), evidence).verdict is Verdict.PASS


def test_checks_and_reviews_stop_at_the_file_page_ceiling():
    check_item = _load("check-runs-page-1.json")["check_runs"][0]
    check_pages = {
        page: {
            "total_count": 4000,
            "check_runs": [
                {**check_item, "id": (page - 1) * 100 + value}
                for value in range(1, 101)
            ],
        }
        for page in range(1, 31)
    }
    review_item = _load("reviews-page-1.json")[0]
    review_pages = {
        page: [
            {**review_item, "id": (page - 1) * 100 + value}
            for value in range(1, 101)
        ]
        for page in range(1, 31)
    }
    transport = FixtureTransport(check_pages=check_pages, review_pages=review_pages)
    evidence = collect_evidence("example-org/example-repo", 42, _policy(), "token-value", transport=transport, clock=_clock)
    check_urls = [url for _method, url in transport.calls if "/check-runs?" in url]
    review_urls = [url for _method, url in transport.calls if "/reviews?" in url]
    assert len(check_urls) == 30
    assert len(review_urls) == 30
    assert evaluate(_policy(), evidence).verdict is Verdict.INDETERMINATE


def test_manifest_is_read_at_the_base_sha():
    class ManifestTransport(FixtureTransport):
        def request(self, method, url, headers):
            parsed = urlsplit(url)
            if "/contents/scope.txt" in parsed.path:
                import base64

                self.calls.append((method, url))
                return self.response({"encoding": "base64", "content": base64.b64encode(b"docs/**\n").decode("ascii")})
            return super().request(method, url, headers)

    transport = ManifestTransport()
    policy = _policy("file:scope.txt")
    evidence = collect_evidence("example-org/example-repo", 42, policy, "token-value", transport=transport, clock=_clock)
    assert evidence.declaration.bound_sha == "2" * 40
    manifest_url = next(url for _method, url in transport.calls if "/contents/" in url)
    assert parse_qs(urlsplit(manifest_url).query) == {"ref": ["2" * 40]}
    assert evaluate(policy, evidence).verdict is Verdict.PASS


def test_transport_names_method_host_and_repository_refusals():
    transport = UrllibTransport("example-org/example-repo")
    with pytest.raises(TransportRefusal, match="GITHUB_METHOD_REFUSED"):
        transport.request("POST", "https://api.github.com/repos/example-org/example-repo/pulls/42", {})
    with pytest.raises(TransportRefusal, match="GITHUB_HOST_REFUSED"):
        transport.request("GET", "https://example.invalid/repos/example-org/example-repo/pulls/42", {})
    with pytest.raises(TransportRefusal, match="GITHUB_REPOSITORY_BOUNDARY_REFUSED"):
        transport.request("GET", "https://api.github.com/repos/another-org/repo/pulls/42", {})


def test_transport_wires_the_redirect_refusal_handler(monkeypatch):
    observed_handlers = []

    def build_opener_probe(*handlers):
        observed_handlers.extend(handlers)
        return object()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener_probe)
    UrllibTransport("example-org/example-repo")
    assert len(observed_handlers) == 1
    assert observed_handlers[0] is _RefuseRedirect
    handler = observed_handlers[0]()
    assert handler.redirect_request(None, None, 302, "redirect", {}, "https://example.invalid") is None


@pytest.mark.parametrize("repository", ["example-org/.", "example-org/.."])
def test_repository_dot_segments_are_refused(repository):
    with pytest.raises(ValueError, match="repository must be owner/name"):
        GitHubReader(repository, "token-value")


def test_reader_never_includes_token_in_a_url():
    transport = FixtureTransport()
    reader = GitHubReader("example-org/example-repo", "credential-sentinel", transport)
    reader.get_json("pulls/42")
    assert all("credential-sentinel" not in url for _method, url in transport.calls)
