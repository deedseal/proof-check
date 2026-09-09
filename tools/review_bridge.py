#!/usr/bin/env python3
"""Publish one exact-head Deedseal COMMENT review from a closed Claude marker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

APP_LOGIN = "deedseal-review-bot[bot]"
OWNER_LOGIN = "avoroncov971-maker"
MARKER = re.compile(
    r"^CLAUDE_REVIEW/v1 head=([0-9a-f]{40}) run=([1-9][0-9]*) "
    r"verdict=(CLEAN|ADVISORY|REPAIR_REQUIRED) findings=([0-9]+)$",
    re.MULTILINE,
)


class Refusal(RuntimeError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


class Transport:
    def request(self, method: str, url: str, token: str, payload: dict | None = None) -> Response:
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "Content-Type": "application/json", "User-Agent": "deedseal-review-bridge/1",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return Response(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read())


def marker(body: str, head: str, run: str) -> tuple[str, int]:
    candidates = [line for line in body.splitlines() if line.startswith("CLAUDE_REVIEW")]
    if len(candidates) != 1:
        raise Refusal("INVALID_SUMMARY_MARKER")
    found = MARKER.fullmatch(candidates[0])
    if found is None:
        raise Refusal("INVALID_SUMMARY_MARKER")
    marked_head, marked_run, verdict, findings = found.groups()
    if marked_head != head or marked_run != run:
        raise Refusal("INVALID_SUMMARY_MARKER")
    count = int(findings)
    if (verdict == "CLEAN" and count != 0) or (verdict == "REPAIR_REQUIRED" and count == 0):
        raise Refusal("INCONSISTENT_FINDINGS")
    if verdict == "REPAIR_REQUIRED" and not re.search(r"(?m)^\s*\d+[.)]\s+\S+", body):
        raise Refusal("MISSING_ACTIONABLE_FINDING")
    return verdict, count


def _json(response: Response) -> Any:
    if not 200 <= response.status < 300:
        raise Refusal("GITHUB_API_FAILURE")
    try:
        return json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("MALFORMED_GITHUB_RESPONSE") from exc


def _get(transport: Transport, api: str, path: str, token: str) -> Any:
    return _json(transport.request("GET", api.rstrip("/") + path, token))


def _pages(transport: Transport, api: str, path: str, token: str):
    for page in range(1, 1001):
        rows = _get(transport, api, f"{path}?per_page=100&page={page}", token)
        if not isinstance(rows, list):
            raise Refusal("MALFORMED_GITHUB_RESPONSE")
        yield from rows
        if len(rows) < 100:
            return
    raise Refusal("PAGINATION_LIMIT")


def review_body(repository: str, pr: int, head: str, run: str, source_id: int, verdict: str, findings: int) -> str:
    stable = "DEEDSEAL_REVIEW_REPAIR_REQUIRED/v1" if verdict == "REPAIR_REQUIRED" else "DEEDSEAL_REVIEW_ACCEPTABLE/v1"
    return ("DEEDSEAL_AUTOMATED_REVIEW/v1\n"
            f"repository={repository}\npr={pr}\nhead={head}\nrun={run}\n"
            f"source_summary_comment_id={source_id}\nverdict={verdict}\nfindings={findings}\n{stable}\n")


def publish(repository: str, pr: int, head: str, run: str, source_id: int, summary: str, token: str,
            *, transport: Transport | None = None, api: str = "https://api.github.com") -> dict:
    transport = transport or Transport()
    if not re.fullmatch(r"[0-9a-f]{40}", head) or not re.fullmatch(r"[1-9][0-9]*", run):
        raise Refusal("INVALID_INPUT")
    verdict, findings = marker(summary, head, run)
    base = "/re" + "pos/" + "/".join(urllib.parse.quote(part) for part in repository.split("/"))
    pull = _get(transport, api, f"{base}/pulls/{pr}", token)
    producer = ((pull.get("user") or {}).get("login") if isinstance(pull, dict) else None)
    if not isinstance(pull, dict) or pull.get("state") != "open" or (pull.get("head") or {}).get("sha") != head:
        raise Refusal("PR_HEAD_CHANGED")
    if producer in {OWNER_LOGIN, APP_LOGIN, "claude[bot]"} or not isinstance(producer, str):
        raise Refusal("UNTRUSTED_PRODUCER")
    body = review_body(repository, pr, head, run, source_id, verdict, findings)
    matches = [review for review in _pages(transport, api, f"{base}/pulls/{pr}/reviews", token)
               if isinstance(review, dict) and review.get("commit_id") == head
               and ((review.get("user") or {}).get("login") == APP_LOGIN)
               and f"run={run}\n" in str(review.get("body") or "")]
    if len(matches) > 1:
        raise Refusal("DUPLICATE_REVIEW")
    if matches:
        if matches[0].get("body") != body or matches[0].get("state") != "COMMENTED":
            raise Refusal("CONFLICTING_REVIEW")
        return {"status": "reconciled", "review_id": matches[0].get("id"), "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
    created = _json(transport.request("POST", api.rstrip("/") + f"{base}/pulls/{pr}/reviews", token,
                                      {"commit_id": head, "event": "COMMENT", "body": body}))
    review_id = created.get("id") if isinstance(created, dict) else None
    if not isinstance(review_id, int):
        raise Refusal("MALFORMED_GITHUB_RESPONSE")
    read_back = _get(transport, api, f"{base}/pulls/{pr}/reviews/{review_id}", token)
    if (not isinstance(read_back, dict) or read_back.get("commit_id") != head or read_back.get("body") != body
            or read_back.get("state") != "COMMENTED" or ((read_back.get("user") or {}).get("login") != APP_LOGIN)):
        raise Refusal("READBACK_MISMATCH")
    return {"status": "published", "review_id": review_id, "body_sha256": hashlib.sha256(body.encode()).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True); parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True); parser.add_argument("--run", required=True)
    parser.add_argument("--summary-id", required=True, type=int); parser.add_argument("--summary-file", required=True)
    parser.add_argument("--token", default=os.environ.get("DEEDSEAL_REVIEW_APP_TOKEN")); parser.add_argument("--api-url", default="https://api.github.com")
    parser.add_argument("--record")
    args = parser.parse_args(argv)
    try:
        if not args.token or args.repository.count("/") != 1 or args.pr < 1 or args.summary_id < 1:
            raise Refusal("INVALID_INPUT")
        result = publish(args.repository, args.pr, args.head, args.run, args.summary_id,
                         open(args.summary_file, encoding="utf-8").read(), args.token, api=args.api_url)
        if args.record:
            with open(args.record, "x", encoding="utf-8") as record:
                json.dump(result, record, sort_keys=True, separators=(",", ":"))
                record.write("\n")
    except (OSError, Refusal) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, separators=(",", ":")))
        return 10
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
