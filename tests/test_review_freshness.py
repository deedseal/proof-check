"""Offline tests for the Review Freshness decision."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "review_freshness"
SPEC = importlib.util.spec_from_file_location(
    "review_freshness_check", ROOT / "tools" / "review_freshness" / "check.py"
)
assert SPEC and SPEC.loader
CHECK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECK
SPEC.loader.exec_module(CHECK)

HEAD = "1" * 40
PR_NUMBER = 19


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureTransport:
    def __init__(self, pages=None, *, reviews=None, status=200, modified=False, missing_ref=None):
        self.modified = modified
        self.missing_ref = missing_ref
        self.pages = pages if pages is not None else {1: []}
        self.status = status
        self.reviews = reviews if reviews is not None else {1: []}
        self.calls = []

    def request(self, url: str, token: str):
        self.calls.append(url)
        path = urlsplit(url).path
        if path.endswith("/contents/.github/workflows/claude-review.yml"):
            ref = parse_qs(urlsplit(url).query)["ref"][0]
            if ref == self.missing_ref:
                return CHECK.Response(404, {}, b"{}")
            blob = "b" * 40 if self.modified and ref == HEAD else "a" * 40
            return CHECK.Response(200, {}, json.dumps({"type": "file", "sha": blob}).encode())
        if path.endswith(f"/pulls/{PR_NUMBER}"):
            return CHECK.Response(200, {}, json.dumps({"head": {"sha": HEAD, "ref": "work/n21"}}).encode())
        if path.endswith("/actions/workflows/claude-review.yml/runs"):
            if self.status != 200:
                return CHECK.Response(self.status, {}, b"{}")
            page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            return CHECK.Response(
                200,
                {},
                json.dumps({"workflow_runs": self.pages.get(page, [])}).encode(),
            )
        if path.endswith(f"/pulls/{PR_NUMBER}/reviews"):
            page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            return CHECK.Response(200, {}, json.dumps(self.reviews.get(page, [])).encode())
        raise AssertionError(f"unexpected endpoint: {url}")


def _review(run_id=1, verdict="CLEAN"):
    stable = "DEEDSEAL_REVIEW_REPAIR_REQUIRED/v1" if verdict == "REPAIR_REQUIRED" else "DEEDSEAL_REVIEW_ACCEPTABLE/v1"
    findings = "1" if verdict == "REPAIR_REQUIRED" else "0"
    return {"id": 44, "commit_id": HEAD, "state": "COMMENTED",
            "user": {"login": "deedseal-review-bot[bot]"},
            "body": ("DEEDSEAL_AUTOMATED_REVIEW/v1\nrepository=deedseal/proof-check\n"
                     f"pr={PR_NUMBER}\nhead={HEAD}\nrun={run_id}\nsource_summary_comment_id=1\n"
                     f"verdict={verdict}\nfindings={findings}\n{stable}\n")}


def _run(runs=None, **kwargs):
    actual_runs = runs or []
    success = next((item for item in actual_runs if item.get("conclusion") == "success"), None)
    reviews = {1: [_review(success.get("id", 1))]} if success else {1: []}
    transport = FixtureTransport({1: actual_runs}, reviews=reviews, **kwargs)
    result = CHECK.decide(
        "deedseal/proof-check",
        PR_NUMBER,
        HEAD,
        "token",
        transport=transport,
        clock=lambda: datetime(2026, 9, 8, tzinfo=timezone.utc),
    )
    return result, transport


def _run_record(**changes):
    record = {
        "id": 1,
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
        "event": "pull_request",
        "path": ".github/workflows/claude-review.yml",
        "created_at": "2026-09-08T00:00:00Z",
        "pull_requests": [{"number": PR_NUMBER}],
    }
    record.update(changes)
    return record


def test_t1_prior_head_run_is_stale():
    result, _ = _run(_load("cases.json")["prior_head"])
    assert (result["decision"], result["reason"]) == ("STALE", "NO_RUN_FOR_HEAD")


@pytest.mark.parametrize("conclusion", ["failure", "skipped"])
def test_t2_failed_or_skipped_run_is_stale(conclusion):
    result, _ = _run([_run_record(conclusion=conclusion)])
    assert (result["decision"], result["reason"]) == ("STALE", "RUN_NOT_SUCCESS")


def test_incomplete_run_is_stale():
    result, _ = _run([_run_record(status="in_progress", conclusion=None)])
    assert (result["decision"], result["reason"]) == ("STALE", "RUN_NOT_COMPLETED")


def test_t3_successful_run_on_head_is_fresh():
    result, _ = _run([_run_record(id=77)])
    assert result["decision"] == "FRESH"
    assert result["reason"] is None
    assert result["matched_run_id"] == 77


def test_repair_review_is_fresh_but_blocking():
    run = _run_record(id=77)
    transport = FixtureTransport({1: [run]}, reviews={1: [_review(77, "REPAIR_REQUIRED")]})
    result = CHECK.decide("deedseal/proof-check", PR_NUMBER, HEAD, "token", transport=transport)
    assert (result["decision"], result["reason"]) == ("BLOCKING", "REVIEW_REPAIR_REQUIRED")


def test_missing_or_duplicate_formal_review_fails_closed():
    run = _run_record()
    for reviews in ([], [_review(), _review()]):
        result = CHECK.decide("deedseal/proof-check", PR_NUMBER, HEAD, "token",
                              transport=FixtureTransport({1: [run]}, reviews={1: reviews}))
        assert (result["decision"], result["reason"]) == ("STALE", "REVIEW_EXECUTION_FAILED")


@pytest.mark.parametrize("modified", [False, True])
def test_t5_t6_reviewer_blob_binding(modified):
    result, transport = _run([_run_record()], modified=modified)
    calls = [url for url in transport.calls if "/contents/" in url]
    assert [parse_qs(urlsplit(url).query)["ref"][0] for url in calls] == [HEAD, "main"]
    assert result["decision"] == ("STALE" if modified else "FRESH")
    assert result["reason"] == ("REVIEWER_WORKFLOW_MODIFIED" if modified else None)


@pytest.mark.parametrize("missing_ref", [HEAD, "main"])
def test_t7_missing_reviewer_blob_fails_closed(missing_ref):
    result, _ = _run([_run_record()], missing_ref=missing_ref)
    assert (result["decision"], result["reason"]) == ("STALE", "API_ERROR")


def test_t4_forged_comment_cannot_supply_evidence():
    result, transport = _run([])
    assert (result["decision"], result["reason"]) == ("STALE", "NO_RUN_FOR_HEAD")
    assert all("comments" not in url for url in transport.calls)


def test_api_5xx_fails_closed():
    result, _ = _run([], status=503)
    assert (result["decision"], result["reason"]) == ("STALE", "API_ERROR")


def test_latest_completed_success_wins():
    result, _ = _run(_load("cases.json")["failure_then_success"])
    assert result["decision"] == "FRESH"
    assert result["matched_run_id"] == 202


def test_latest_completed_failure_supersedes_older_success():
    runs = [
        _run_record(id=201, created_at="2026-09-08T00:01:00Z"),
        _run_record(id=202, created_at="2026-09-08T00:02:00Z", conclusion="failure"),
    ]
    result, _ = _run(runs)
    assert (result["decision"], result["reason"]) == ("STALE", "RUN_NOT_SUCCESS")
    assert result["matched_run_id"] == 202


def test_pagination_beyond_one_page():
    page_one = [_run_record(id=value, head_sha="2" * 40) for value in range(100)]
    page_two = _load("pagination-page-2.json")["workflow_runs"]
    transport = FixtureTransport({1: page_one, 2: page_two}, reviews={1: [_review(302)]})
    result = CHECK.decide(
        "deedseal/proof-check", PR_NUMBER, HEAD, "token", transport=transport
    )
    assert result["decision"] == "FRESH"
    run_calls = [url for url in transport.calls if "/runs?" in url]
    assert [parse_qs(urlsplit(url).query)["page"] for url in run_calls] == [["1"], ["2"]]


@pytest.mark.parametrize(
    "changes",
    [
        {"event": "workflow_dispatch"},
        {"path": ".github/workflows/forged.yml"},
        {"pull_requests": [{"number": 20}]},
    ],
)
def test_wrong_event_path_or_pr_cannot_make_fresh(changes):
    result, _ = _run([_run_record(**changes)])
    assert (result["decision"], result["reason"]) == ("STALE", "NO_RUN_FOR_HEAD")


def test_head_moved_since_event_is_stale():
    result, _ = _run([])
    transport = FixtureTransport({1: [_run_record()]})
    result = CHECK.decide(
        "deedseal/proof-check", PR_NUMBER, "2" * 40, "token", transport=transport
    )
    assert (result["decision"], result["reason"]) == ("STALE", "NO_RUN_FOR_HEAD")


@pytest.mark.parametrize(
    "argv",
    [
        ["--repository", "bad", "--pr-number", "19", "--head-sha", HEAD, "--token", "x"],
        ["--repository", "a/b", "--pr-number", "0", "--head-sha", HEAD, "--token", "x"],
        ["--repository", "a/b", "--pr-number", "19", "--head-sha", "bad", "--token", "x"],
    ],
)
def test_invalid_configuration_exits_two(argv, capsys):
    assert CHECK.main(argv) == 2
    assert capsys.readouterr().out == ""
