"""Offline behavior tests for the trusted formal-review bridge."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

SPEC = importlib.util.spec_from_file_location("review_bridge", Path(__file__).parents[1] / "tools/review_bridge.py")
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BRIDGE
SPEC.loader.exec_module(BRIDGE)

HEAD, RUN = "a" * 40, "12"
SUMMARY = f"Reviewed.\n\nCLAUDE_REVIEW/v1 head={HEAD} run={RUN} verdict=CLEAN findings=0\n"


class RecordingTransport:
    def __init__(self, *, existing=None, existing_pages=None, readback=None, pull=None):
        self.existing = existing or []
        self.existing_pages = existing_pages
        self.readback, self.calls = readback, []
        self.pull = pull or {"state": "open", "head": {"sha": HEAD}, "user": {"login": "author"}}
    def request(self, method, url, token, payload=None):
        self.calls.append((method, url, payload))
        path = urlsplit(url).path
        if method == "GET" and path.endswith("/pulls/7"):
            return BRIDGE.Response(200, json.dumps(self.pull).encode())
        if method == "GET" and path.endswith("/reviews"):
            page = int((urlsplit(url).query.split("page=")[-1] or "1"))
            rows = self.existing if self.existing_pages is None else self.existing_pages[page - 1]
            return BRIDGE.Response(200, json.dumps(rows).encode())
        if method == "POST":
            return BRIDGE.Response(200, b'{"id":91}')
        if method == "GET" and path.endswith("/reviews/91"):
            body = payload if self.readback is None else self.readback
            expected = next(call[2]["body"] for call in self.calls if call[0] == "POST")
            return BRIDGE.Response(200, json.dumps(body or {"id": 91, "commit_id": HEAD, "state": "COMMENTED", "body": expected, "user": {"login": BRIDGE.APP_LOGIN}}).encode())
        raise AssertionError((method, path))


def test_clean_publishes_fixed_comment_at_exact_head_and_readback():
    transport = RecordingTransport()
    result = BRIDGE.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token", transport=transport)
    post = next(payload for method, _, payload in transport.calls if method == "POST")
    assert result["status"] == "published"
    assert post["event"] == "COMMENT" and post["commit_id"] == HEAD
    assert "DEEDSEAL_REVIEW_ACCEPTABLE/v1" in post["body"]


@pytest.mark.parametrize("summary", [
    SUMMARY.replace("findings=0", "findings=1"),
    SUMMARY.replace("CLEAN", "UNKNOWN"),
    SUMMARY + SUMMARY,
    SUMMARY + "CLAUDE_REVIEW/v1 unknown\n",
    f"CLAUDE_REVIEW/v1 head={HEAD} run={RUN} verdict=REPAIR_REQUIRED findings=1\n",
])
def test_bad_closed_marker_refuses(summary):
    with pytest.raises(BRIDGE.Refusal):
        BRIDGE.publish("owner/repo", 7, HEAD, RUN, 8, summary, "token", transport=RecordingTransport())


def test_exact_retry_reconciles_without_write():
    body = BRIDGE.review_body("owner/repo", 7, HEAD, RUN, 8, "CLEAN", 0)
    existing = [{"id": 4, "commit_id": HEAD, "state": "COMMENTED", "body": body, "user": {"login": BRIDGE.APP_LOGIN}}]
    transport = RecordingTransport(existing=existing)
    assert BRIDGE.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token", transport=transport)["status"] == "reconciled"
    assert all(method != "POST" for method, _, _ in transport.calls)


def test_conflicts_and_readback_mismatch_refuse():
    conflicting = [{"id": 4, "commit_id": HEAD, "state": "COMMENTED", "body": "run=12\nother", "user": {"login": BRIDGE.APP_LOGIN}}]
    with pytest.raises(BRIDGE.Refusal):
        BRIDGE.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token", transport=RecordingTransport(existing=conflicting))
    with pytest.raises(BRIDGE.Refusal):
        BRIDGE.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token", transport=RecordingTransport(readback={"id": 91}))


@pytest.mark.parametrize("producer", [BRIDGE.OWNER_LOGIN, BRIDGE.APP_LOGIN, "claude[bot]", None])
def test_hostile_producer_identities_refuse(producer):
    pull = {"state": "open", "head": {"sha": HEAD}, "user": {"login": producer}}
    with pytest.raises(BRIDGE.Refusal, match="UNTRUSTED_PRODUCER"):
        BRIDGE.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token", transport=RecordingTransport(pull=pull))


@pytest.mark.parametrize("pull", [
    {"state": "open", "head": {"sha": "b" * 40}, "user": {"login": "author"}},
    {"state": "closed", "head": {"sha": HEAD}, "user": {"login": "author"}},
])
def test_moved_head_and_closed_pr_refuse(pull):
    with pytest.raises(BRIDGE.Refusal, match="PR_HEAD_CHANGED"):
        BRIDGE.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token", transport=RecordingTransport(pull=pull))


def test_review_reconciliation_paginates_before_refusing_or_publishing():
    body = BRIDGE.review_body("owner/repo", 7, HEAD, RUN, 8, "CLEAN", 0)
    first_page = [{"id": number, "commit_id": "b" * 40, "state": "COMMENTED", "body": "other",
                   "user": {"login": BRIDGE.APP_LOGIN}} for number in range(100)]
    exact = {"id": 101, "commit_id": HEAD, "state": "COMMENTED", "body": body,
             "user": {"login": BRIDGE.APP_LOGIN}}
    transport = RecordingTransport(existing_pages=[first_page, [exact]])
    result = BRIDGE.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token", transport=transport)
    assert result["status"] == "reconciled" and result["review_id"] == 101
    assert all(method != "POST" for method, _, _ in transport.calls)
    assert [urlsplit(url).query for method, url, _ in transport.calls if urlsplit(url).path.endswith("/reviews")] == [
        "per_page=100&page=1", "per_page=100&page=2"]


def _mutant(tmp_path, old, new):
    source = (Path(__file__).parents[1] / "tools/review_bridge.py").read_text()
    assert source.count(old) == 1
    path = tmp_path / "mutant.py"
    path.write_text(source.replace(old, new))
    spec = importlib.util.spec_from_file_location("review_bridge_mutant_" + str(abs(hash(old + new))), path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_behavioral_mutants_kill_required_bridge_guards(tmp_path):
    class GuardTransport(RecordingTransport):
        def request(self, method, url, token, payload=None):
            if method == "POST":
                assert payload.get("commit_id") == HEAD and payload.get("event") == "COMMENT"
            return super().request(method, url, token, payload)
    for old, new in [('"commit_id": head, "event": "COMMENT"', '"event": "COMMENT"'),
                     ('"event": "COMMENT"', '"event": "APPROVE"')]:
        module = _mutant(tmp_path, old, new)
        with pytest.raises(AssertionError):
            module.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token", transport=GuardTransport())
    module = _mutant(tmp_path, 'if producer in {OWNER_LOGIN, APP_LOGIN, "claude[bot]"} or not isinstance(producer, str):',
                     'if False:')
    hostile_pull = {"state": "open", "head": {"sha": HEAD}, "user": {"login": module.APP_LOGIN}}
    with pytest.raises(AssertionError):
        assert module.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token",
                              transport=RecordingTransport(pull=hostile_pull)) is None
    module = _mutant(tmp_path, '(verdict == "CLEAN" and count != 0)', 'False')
    with pytest.raises(AssertionError):
        assert module.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY.replace("findings=0", "findings=1"), "token", transport=RecordingTransport()) is None
    module = _mutant(tmp_path, 'if len(matches) > 1:', 'if False:')
    body = module.review_body("owner/repo", 7, HEAD, RUN, 8, "CLEAN", 0)
    duplicate = [{"id": n, "commit_id": HEAD, "state": "COMMENTED", "body": body, "user": {"login": module.APP_LOGIN}} for n in (1, 2)]
    with pytest.raises(AssertionError):
        assert module.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token", transport=RecordingTransport(existing=duplicate)) is None
    module = _mutant(tmp_path, 'or read_back.get("body") != body', 'or False')
    bad = {"id": 91, "commit_id": HEAD, "state": "COMMENTED", "body": "wrong", "user": {"login": module.APP_LOGIN}}
    with pytest.raises(AssertionError):
        assert module.publish("owner/repo", 7, HEAD, RUN, 8, SUMMARY, "token", transport=RecordingTransport(readback=bad)) is None
