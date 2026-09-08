#!/usr/bin/env python3
"""Observe the pre-Owner acceptance boundary for one exact PR head."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def _get(repo: str, suffix: str, token: str) -> object:
    url = f"https://api.github.com/repos/{repo}/{suffix}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def evaluate(artifact: dict) -> dict:
    head = artifact["head_sha"]
    required = set(artifact["required_checks"])
    successful = {
        item["name"]
        for item in artifact["checks"]
        if item.get("head_sha") == head and item.get("conclusion") == "success"
    }
    after_push = artifact.get("events_after_agent_push", [])
    human_after_push = any(item.get("actor_type") == "User" for item in after_push)
    conditions = {
        "pr_open": artifact.get("pr_state") == "open",
        "mergeable_clean": artifact.get("mergeable") is True and artifact.get("mergeable_state") == "clean",
        "required_checks_success": required <= successful,
        "no_human_activity_after_agent_push": not human_after_push,
        "receipt_verified": artifact.get("arm") != "A" or artifact.get("receipt_verified") is True,
    }
    return {"reached_B0": all(conditions.values()), "conditions": conditions, "head_sha": head}


def capture(args: argparse.Namespace) -> dict:
    token = os.environ.get(args.token_env, "")
    if not token:
        raise ValueError(f"missing token environment variable: {args.token_env}")
    pull = _get(args.repo, f"pulls/{args.pr}", token)
    if not isinstance(pull, dict) or pull.get("head", {}).get("sha") != args.head:
        raise ValueError("PR head does not match the pre-registered head")
    checks_doc = _get(args.repo, f"commits/{args.head}/check-runs?per_page=100", token)
    if not isinstance(checks_doc, dict) or not isinstance(checks_doc.get("check_runs"), list):
        raise ValueError("check-runs response is invalid")
    timeline = _get(args.repo, f"issues/{args.pr}/timeline?per_page=100", token)
    if not isinstance(timeline, list):
        raise ValueError("timeline response is invalid")
    last_push = max(
        (item.get("created_at", "") for item in timeline if item.get("event") == "committed"),
        default="",
    )
    relevant = {"reviewed", "commented", "labeled", "unlabeled", "committed", "head_ref_force_pushed"}
    after = []
    for item in timeline:
        at = item.get("created_at", "")
        if at > last_push and item.get("event") in relevant:
            actor = item.get("actor") or item.get("user") or {}
            after.append({"event": item.get("event"), "at": at, "actor_type": actor.get("type")})
    receipt_verified = False
    if args.arm == "A":
        if not args.receipt or not args.offline_bundle:
            raise ValueError("Arm A requires --receipt and --offline-bundle")
        completed = subprocess.run(
            [sys.executable, "-m", "proof_check", "verify", args.receipt, "--offline-bundle", args.offline_bundle],
            check=False,
            capture_output=True,
            text=True,
        )
        receipt_verified = completed.returncode == 0
    return {
        "arm": args.arm,
        "head_sha": args.head,
        "pr_state": pull.get("state"),
        "mergeable": pull.get("mergeable"),
        "mergeable_state": pull.get("mergeable_state"),
        "required_checks": json.loads(Path(args.required_checks).read_text()),
        "checks": checks_doc["check_runs"],
        "events_after_agent_push": after,
        "receipt_verified": receipt_verified,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact")
    parser.add_argument("--repo")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--head")
    parser.add_argument("--arm", choices=("A", "B"))
    parser.add_argument("--required-checks")
    parser.add_argument("--receipt")
    parser.add_argument("--offline-bundle")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        fixture = json.loads(Path("tests/fixtures/github_read/pull.json").read_text())
        assert isinstance(fixture["head"]["sha"], str)
        print("BOUNDARY_SELF_TEST_OK")
        return 0
    artifact = json.loads(Path(args.artifact).read_text()) if args.artifact else capture(args)
    print(json.dumps(evaluate(artifact), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
