#!/usr/bin/env python3
"""Decide whether the reviewer workflow succeeded on the current PR head."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

REVIEWER_WORKFLOW = "claude-review.yml"
REVIEWER_PATH = ".github/workflows/claude-review.yml"
REASONS = {
    "REVIEWER_WORKFLOW_MODIFIED",
    "NO_RUN_FOR_HEAD",
    "RUN_NOT_COMPLETED",
    "RUN_NOT_SUCCESS",
    "API_ERROR",
    "REVIEW_EXECUTION_FAILED",
    "REVIEW_REPAIR_REQUIRED",
}
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
APP_LOGIN = "deedseal-review-bot[bot]"
REVIEW_RE = re.compile(
    r"\ADEEDSEAL_AUTOMATED_REVIEW/v1\nrepository=([^\n]+)\npr=([1-9][0-9]*)\n"
    r"head=([0-9a-f]{40})\nrun=([1-9][0-9]*)\nsource_summary_comment_id=([1-9][0-9]*)\n"
    r"verdict=(CLEAN|ADVISORY|REPAIR_REQUIRED)\nfindings=([0-9]+)\n"
    r"(DEEDSEAL_REVIEW_ACCEPTABLE/v1|DEEDSEAL_REVIEW_REPAIR_REQUIRED/v1)\n\Z"
)


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


class UrllibTransport:
    def request(self, url: str, token: str) -> Response:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "deedseal-review-freshness/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return Response(
                    response.status,
                    {key.lower(): value for key, value in response.headers.items()},
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            return Response(
                exc.code,
                {key.lower(): value for key, value in exc.headers.items()},
                exc.read(),
            )


def _json(response: Response) -> object:
    if response.status < 200 or response.status >= 300:
        raise RuntimeError(f"GitHub API returned HTTP {response.status}")
    try:
        return json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("GitHub API returned invalid JSON") from exc


def _get(
    transport: UrllibTransport,
    api_url: str,
    path: str,
    token: str,
    query: dict[str, str] | None = None,
) -> object:
    url = f"{api_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    return _json(transport.request(url, token))


def _associated_with_pr(run: dict, pr_number: int) -> bool:
    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list):
        return False
    return any(isinstance(pr, dict) and pr.get("number") == pr_number for pr in pull_requests)


def _run_key(run: dict) -> tuple[str, int, int, int]:
    return (
        str(run.get("created_at") or ""),
        int(run.get("run_number") or 0),
        int(run.get("run_attempt") or 0),
        int(run.get("id") or 0),
    )


def _review_verdict(review: dict, repository: str, pr: int, head: str, run_id: object) -> str | None:
    if (review.get("commit_id") != head or review.get("state") != "COMMENTED"
            or ((review.get("user") or {}).get("login") != APP_LOGIN)):
        return None
    found = REVIEW_RE.fullmatch(str(review.get("body") or ""))
    if not found:
        return None
    repo, body_pr, body_head, body_run, _source, verdict, findings, stable = found.groups()
    if repo != repository or int(body_pr) != pr or body_head != head or str(run_id) != body_run:
        return None
    if (verdict == "CLEAN" and findings != "0") or (verdict == "REPAIR_REQUIRED" and findings == "0"):
        return None
    if (verdict == "REPAIR_REQUIRED") != (stable == "DEEDSEAL_REVIEW_REPAIR_REQUIRED/v1"):
        return None
    return verdict


def decide(
    repository: str,
    pr_number: int,
    head_sha: str,
    token: str,
    *,
    transport: UrllibTransport | None = None,
    api_url: str = "https://api.github.com",
    clock: Callable[[], datetime] | None = None,
) -> dict:
    transport = transport or UrllibTransport()
    clock = clock or (lambda: datetime.now(timezone.utc))
    owner, repo = repository.split("/", 1)
    base = f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"

    try:
        pull = _get(transport, api_url, f"{base}/pulls/{pr_number}", token)
        if not isinstance(pull, dict):
            raise RuntimeError("pull response is not an object")
        current_sha = str(((pull.get("head") or {}).get("sha") or ""))
        head_branch = str(((pull.get("head") or {}).get("ref") or ""))
        if current_sha != head_sha or not head_branch:
            return _result("STALE", "NO_RUN_FOR_HEAD", None, current_sha or head_sha, clock)

        runs: list[dict] = []
        for page in range(1, 101):
            payload = _get(
                transport,
                api_url,
                f"{base}/actions/workflows/{REVIEWER_WORKFLOW}/runs",
                token,
                {
                    "branch": head_branch,
                    "event": "pull_request",
                    "per_page": "100",
                    "page": str(page),
                },
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
                raise RuntimeError("workflow-runs response is malformed")
            page_runs = payload["workflow_runs"]
            runs.extend(run for run in page_runs if isinstance(run, dict))
            if len(page_runs) < 100:
                break
        else:
            raise RuntimeError("workflow-runs pagination exceeded 100 pages")
    except Exception:
        return _result("STALE", "API_ERROR", None, head_sha, clock)

    matching = [
        run
        for run in runs
        if run.get("head_sha") == head_sha
        and run.get("event") == "pull_request"
        and run.get("path") == REVIEWER_PATH
        and _associated_with_pr(run, pr_number)
    ]
    if not matching:
        return _result("STALE", "NO_RUN_FOR_HEAD", None, head_sha, clock)

    latest = max(matching, key=_run_key)
    run_id = latest.get("id")
    if latest.get("status") != "completed":
        return _result("STALE", "RUN_NOT_COMPLETED", run_id, head_sha, clock)
    if latest.get("conclusion") != "success":
        return _result("STALE", "RUN_NOT_SUCCESS", run_id, head_sha, clock)
    try:
        blobs = []
        for ref in (head_sha, "main"):
            content = _get(
                transport, api_url, f"{base}/contents/{REVIEWER_PATH}",
                token, {"ref": ref},
            )
            if (
                not isinstance(content, dict)
                or content.get("type") != "file"
                or not isinstance(content.get("sha"), str)
                or not SHA_RE.fullmatch(content["sha"])
            ):
                raise RuntimeError("invalid reviewer workflow blob")
            blobs.append(content["sha"])
    except Exception:
        return _result("STALE", "API_ERROR", run_id, head_sha, clock)
    if blobs[0] != blobs[1]:
        return _result("STALE", "REVIEWER_WORKFLOW_MODIFIED", run_id, head_sha, clock)
    try:
        reviews: list[dict] = []
        for page in range(1, 101):
            payload = _get(transport, api_url, f"{base}/pulls/{pr_number}/reviews", token,
                           {"per_page": "100", "page": str(page)})
            if not isinstance(payload, list):
                raise RuntimeError("reviews response is malformed")
            reviews.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                break
        else:
            raise RuntimeError("reviews pagination exceeded 100 pages")
    except Exception:
        return _result("STALE", "API_ERROR", run_id, head_sha, clock)
    verdicts = [verdict for review in reviews
                if (verdict := _review_verdict(review, repository, pr_number, head_sha, run_id))]
    if len(verdicts) != 1:
        return _result("STALE", "REVIEW_EXECUTION_FAILED", run_id, head_sha, clock)
    if verdicts[0] == "REPAIR_REQUIRED":
        return _result("BLOCKING", "REVIEW_REPAIR_REQUIRED", run_id, head_sha, clock)
    return _result("FRESH", None, run_id, head_sha, clock)


def _result(
    decision: str,
    reason: str | None,
    run_id: object,
    head_sha: str,
    clock: Callable[[], datetime],
) -> dict:
    if reason is not None and reason not in REASONS:
        raise ValueError(f"unknown reason: {reason}")
    observed = clock()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return {
        "decision": decision,
        "reason": reason,
        "matched_run_id": run_id,
        "head_sha": head_sha,
        "observed_at": observed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="owner/repository")
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        args.repository.count("/") != 1
        or not all(args.repository.split("/"))
        or args.pr_number < 1
        or not SHA_RE.fullmatch(args.head_sha)
        or not args.token
    ):
        print("invalid repository, PR number, head SHA, or missing token", file=sys.stderr)
        return 2
    result = decide(
        args.repository,
        args.pr_number,
        args.head_sha.lower(),
        args.token,
        api_url=args.api_url,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["decision"] == "FRESH" else 10


if __name__ == "__main__":
    raise SystemExit(main())
