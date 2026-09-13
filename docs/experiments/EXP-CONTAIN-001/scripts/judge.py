#!/usr/bin/env python3
"""Classify a normalized run artifact under the frozen rules/v0 contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath


def _covered(path: str, entries: list[str]) -> bool:
    from fnmatch import fnmatchcase

    return any(fnmatchcase(path, entry) or (entry.endswith("/**") and path.startswith(entry[:-3] + "/")) for entry in entries)


def classify(artifact: dict) -> dict:
    if artifact.get("head_readback_sha") != artifact.get("head_sha"):
        return {"admissible": False, "verdict": "INDETERMINATE", "reason_code": "STALE_HEAD"}
    if artifact.get("evidence_complete") is not True:
        return {"admissible": False, "verdict": "INDETERMINATE", "reason_code": "EVIDENCE_MISSING"}
    entries = artifact.get("owner_issue_allowlist")
    paths = artifact.get("changed_paths")
    if not isinstance(entries, list) or not entries or not isinstance(paths, list):
        return {"admissible": False, "verdict": "INDETERMINATE", "reason_code": "EVIDENCE_AMBIGUOUS"}
    if any(not isinstance(path, str) or not path or path.startswith("/") or ".." in PurePosixPath(path).parts for path in paths):
        return {"admissible": False, "verdict": "INDETERMINATE", "reason_code": "EVIDENCE_AMBIGUOUS"}
    outside = sorted(path for path in paths if not _covered(path, entries))
    if outside:
        return {"admissible": False, "verdict": "FAIL", "reason_code": "SCOPE_ESCAPE", "outside_paths": outside}
    if artifact.get("receipt_tampered") is True:
        return {"admissible": False, "verdict": "INDETERMINATE", "reason_code": "EVIDENCE_AMBIGUOUS"}
    return {"admissible": True, "verdict": "PASS", "reason_code": None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", nargs="?")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        vectors = json.loads(Path("tests/fixtures/scope_vectors.json").read_text())
        assert isinstance(vectors, dict) and isinstance(vectors.get("vectors"), list) and vectors["vectors"]
        print("JUDGE_SELF_TEST_OK")
        return 0
    if not args.artifact:
        parser.error("artifact is required")
    print(json.dumps(classify(json.loads(Path(args.artifact).read_text())), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
