"""Offline probes of the Issue-bound scope example workflow."""

import hashlib
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / "examples/issue-scope/proof-check.yml").read_text()
README = (ROOT / "examples/issue-scope/README.md").read_text()
DECLARE = "Resolve the Scope-Issue declaration into a pinned policy"

def step(name):
    match = re.search(rf"^      - name: {re.escape(name)}\n(.*?)(?=^      - name: |\Z)",
                       WORKFLOW, re.MULTILINE | re.DOTALL)
    assert match, name
    return match.group(1)

def program(name):
    match = re.search(r"          python3 -I - <<'PY'\n(.*?)^          PY$",
                       step(name), re.MULTILINE | re.DOTALL)
    assert match, name
    return textwrap.dedent(match.group(1))

def test_structural_invariants_are_preserved():
    assert "types: [opened, synchronize, reopened, edited]" in WORKFLOW
    assert "permissions:\n  contents: read\n  pull-requests: read\n  issues: read\n" in WORKFLOW
    uses = re.findall(r"^\s+uses: (\S+)", WORKFLOW, re.MULTILINE)
    assert uses == ["deedseal/proof-check@363aad91142a01df6e5d72a87495d2f09be28823",
                     "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"]
    assert "secrets." not in WORKFLOW
    action = step("Evaluate the PR head against the Issue-bound scope")
    assert "if: ${{ steps.declare.outcome == 'success' }}" in action and "target: head" in action
    assert "if: always()" in step("Upload offline-verifiable bundle")
    assert "UnicodeEncodeError" in program(DECLARE)
    for required in (
        "binds the receipt to the Issue-body bytes",
        "does not prove who originally wrote the Issue",
        "fresh workflow run to produce new evidence",
        "not code correctness or merge approval",
    ):
        assert required in README
    claims = (ROOT / "docs/contract/claims-and-nonclaims.md").read_text()
    gated = claims.split("## Prohibited or gated vocabulary", 1)[1]
    terms = re.findall(r"`([^`]+)`", next(line for line in gated.splitlines() if line.startswith("`")))
    pattern = re.compile(r"(?i)(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, terms)) + r")(?![A-Za-z0-9_])")
    assert pattern.search(README) is None and pattern.search(WORKFLOW) is None

def run_declare(tmp_path, *, pr_body, issue):
    (tmp_path / "event.json").write_text(json.dumps({"pull_request": {"body": pr_body}}))
    gh = tmp_path / "gh"
    gh.write_text("#!/usr/bin/env python3\nimport sys\n"
                   "assert sys.argv[1:3] == ['api', 'repos/owner/repo/issues/7']\n"
                   f"print({json.dumps(json.dumps(issue))})\n")
    gh.chmod(0o755)
    return subprocess.run(
        ["python3", "-I", "-"], input=program(DECLARE), cwd=tmp_path, text=True, capture_output=True,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "GITHUB_EVENT_PATH": str(tmp_path / "event.json"), "REVIEW_REPOSITORY": "owner/repo"})

def test_live_issue_allowlist_pins_the_exact_declaration_bytes(tmp_path):
    body = "## Allowlist\nexamples/issue-scope/proof-check.yml\nREADME.md\n"
    result = run_declare(tmp_path, pr_body="Scope-Issue: #7\n", issue={"state": "open", "body": body})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bundle/declaration.txt").read_bytes() == body.encode("utf-8")
    policy = json.loads((tmp_path / "bundle/policy.json").read_text())
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert policy["scope"] == {"source": f"pinned:{digest}", "trust": "advisory"}
    assert policy["reviews"] == {"policy": "report_only"}

@pytest.mark.parametrize(("pr_body", "issue"), [
    ("", {"state": "open", "body": "## Allowlist\na\n"}),  # no Scope-Issue line
    ("Scope-Issue: #7\nScope-Issue: #8\n", {"state": "open", "body": "## Allowlist\na\n"}),  # duplicated
    ("Scope-Issue: other/repo#7\n", {"state": "open", "body": "## Allowlist\na\n"}),  # cross-repository
    ("Scope-Issue: #7\n", {"state": "closed", "body": "## Allowlist\na\n"}),  # closed Issue
    ("Scope-Issue: #7\n", {"state": "open", "pull_request": {}, "body": "## Allowlist\na\n"}),  # is a PR
    ("Scope-Issue: #7\n", {"state": "open", "body": None}),  # missing body
    ("Scope-Issue: #7\n", {"state": "open", "body": "no heading here\n"}),  # no section
    ("Scope-Issue: #7\n", {"state": "open", "body": "## Allowlist\n## Allowlist\na\n"}),  # duplicate section
    ("Scope-Issue: #7\n", {"state": "open", "body": "## Allowlist\n"}),  # empty section
    ("Scope-Issue: #7\n", {"state": "open", "body": "## Allowlist\na\na\n"}),  # duplicate entry
    ("Scope-Issue: #7\n", {"state": "open", "body": "stray prose\n## Allowlist\na\n"}),  # content outside
])
def test_malformed_reference_or_declaration_fails_closed(tmp_path, pr_body, issue):
    result = run_declare(tmp_path, pr_body=pr_body, issue=issue)
    assert result.returncode == 2
    assert not (tmp_path / "bundle").exists()
