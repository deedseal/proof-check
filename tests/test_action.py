"""Composite Action metadata and shell-wrapper behavior."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from proof_check.contracts import validate_policy

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "action" / "run.sh"
PIN = "a" * 40
TOKEN = "credential-sentinel"


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_tools(tmp_path: Path) -> Path:
    tools = tmp_path / "bin"
    tools.mkdir()
    fake_proof_check = tools / "proof-check"
    _executable(
        fake_proof_check,
        f"""#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
receipt = pathlib.Path(args[args.index("--receipt") + 1])
code = int(os.environ["FAKE_EXIT_CODE"])
verdict = {{0: "PASS", 10: "FAIL", 20: "INDETERMINATE", 2: "INDETERMINATE"}}[code]
document = {{
    "verdict": verdict,
    "reason_codes": [] if code == 0 else ["EVIDENCE_MISSING"],
    "tool": {{"source_commit": os.environ["FAKE_SOURCE_COMMIT"]}},
}}
receipt.write_bytes(json.dumps(document, separators=(",", ":")).encode())
print(f"{{verdict}}: fixture")
raise SystemExit(code)
""",
    )
    venv_python = tools / "venv-python"
    _executable(
        venv_python,
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then exit 0; fi
exec "$REAL_PYTHON" "$@"
""",
    )
    _executable(
        tools / "python3",
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
  mkdir -p "$3/bin"
  cp "$FAKE_VENV_PYTHON" "$3/bin/python"
  cp "$FAKE_PROOF_CHECK" "$3/bin/proof-check"
  chmod +x "$3/bin/python" "$3/bin/proof-check"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
""",
    )
    _executable(
        tools / "git",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_GIT_LOG"
if [[ "${1:-}" == "clone" ]]; then mkdir -p "${!#}"; fi
if [[ "$*" == *"rev-parse FETCH_HEAD"* || "$*" == *"rev-parse HEAD"* ]]; then
  printf '%s\n' "$FAKE_SOURCE_COMMIT"
fi
""",
    )
    return tools


def _run(tmp_path: Path, *, exit_code: int = 0, ref: str = PIN, pr: str = "7", event: str = "pull_request"):
    tools = _fake_tools(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    git_log = tmp_path / "git.log"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{tools}:{environment['PATH']}",
            "REAL_PYTHON": sys.executable,
            "FAKE_VENV_PYTHON": str(tools / "venv-python"),
            "FAKE_PROOF_CHECK": str(tools / "proof-check"),
            "FAKE_GIT_LOG": str(git_log),
            "FAKE_SOURCE_COMMIT": PIN,
            "FAKE_EXIT_CODE": str(exit_code),
            "GITHUB_ACTION_REF": ref,
            "GITHUB_ACTION_REPOSITORY": "deedseal/proof-check" if ref else "",
            "GITHUB_ACTION_PATH": str(workspace),
            "GITHUB_WORKSPACE": str(workspace),
            "GITHUB_SHA": PIN,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "RUNNER_TEMP": str(tmp_path),
            "PROOF_CHECK_POLICY": "policy.json",
            "PROOF_CHECK_RECEIPT": "proof-check-receipt.json",
            "PROOF_CHECK_TARGET": "head",
            "PROOF_CHECK_DECLARATION": "",
            "PROOF_CHECK_PR": pr,
            "PROOF_CHECK_REPO": "example-org/example-repo",
            "PROOF_CHECK_GITHUB_TOKEN": TOKEN,
        }
    )
    result = subprocess.run(
        ["bash", str(RUNNER)], cwd=workspace, env=environment, capture_output=True, text=True
    )
    return result, workspace, output, summary, git_log


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize("ref", ["main", "v1", "a" * 39])
def test_non_commit_ref_is_refused_before_clone(tmp_path, ref):
    result, _workspace, _output, _summary, git_log = _run(tmp_path, ref=ref)
    assert result.returncode == 2
    assert "full 40-hex commit" in result.stderr
    assert not git_log.exists()


def test_empty_pull_request_is_refused_before_clone(tmp_path):
    result, _workspace, _output, _summary, git_log = _run(tmp_path, pr="")
    assert result.returncode == 2
    assert "pull request number is empty" in result.stderr
    assert not git_log.exists()


@pytest.mark.parametrize(("exit_code", "verdict"), [(0, "PASS"), (10, "FAIL"), (20, "INDETERMINATE"), (2, "INDETERMINATE")])
def test_cli_exit_and_receipt_outputs_are_preserved(tmp_path, exit_code, verdict):
    result, workspace, output, summary, _git_log = _run(tmp_path, exit_code=exit_code)
    receipt = workspace / "proof-check-receipt.json"
    values = _outputs(output)
    assert result.returncode == exit_code
    assert values == {
        "verdict": verdict,
        "exit-code": str(exit_code),
        "receipt-path": "proof-check-receipt.json",
        "receipt-digest": hashlib.sha256(receipt.read_bytes()).hexdigest(),
    }
    observed = result.stdout + result.stderr + output.read_text() + summary.read_text() + receipt.read_text()
    assert TOKEN not in observed
    assert PIN in summary.read_text(encoding="utf-8")


def test_repository_local_action_uses_the_workflow_sha(tmp_path):
    result, _workspace, output, _summary, _git_log = _run(tmp_path, ref="")
    assert result.returncode == 0
    assert _outputs(output)["verdict"] == "PASS"


def test_action_metadata_policy_and_workflow_contract():
    metadata = json.loads((REPO_ROOT / "action.yml").read_text(encoding="utf-8"))
    assert metadata["runs"]["using"] == "composite"
    assert len(metadata["runs"]["steps"]) == 1
    assert metadata["runs"]["steps"][0]["run"] == 'bash "${GITHUB_ACTION_PATH}/action/run.sh"'
    assert set(metadata["inputs"]) == {"policy", "receipt", "target", "declaration", "pr", "repo", "token"}
    assert set(metadata["outputs"]) == {"verdict", "exit-code", "receipt-path", "receipt-digest"}
    assert metadata["inputs"]["policy"]["required"] is True
    assert metadata["inputs"]["receipt"]["default"] == "proof-check-receipt.json"
    assert metadata["inputs"]["target"]["default"] == "head"
    validate_policy(json.loads((REPO_ROOT / "examples" / "action" / "policy.json").read_text()))

    workflow = (REPO_ROOT / ".github" / "workflows" / "proof-check-selftest.yml").read_text()
    permissions = re.search(r"(?ms)^permissions:\n((?:  [^\n]+\n)+)", workflow).group(1)
    assert set(re.findall(r"^  ([a-z-]+): read$", permissions, re.MULTILINE)) == {
        "contents", "pull-requests", "checks", "statuses"
    }
    assert re.search(r"uses: actions/checkout@[0-9a-f]{40} # v", workflow)


def test_public_action_files_avoid_contract_gated_terms():
    claims = (REPO_ROOT / "docs" / "contract" / "claims-and-nonclaims.md").read_text()
    gated_section = claims.split("## Prohibited or gated vocabulary", 1)[1]
    vocabulary_line = next(line for line in gated_section.splitlines() if line.startswith("`"))
    terms = re.findall(r"`([^`]+)`", vocabulary_line)
    pattern = re.compile(r"(?i)(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, terms)) + r")(?![A-Za-z0-9_])")
    for relative in ("action.yml", "action/run.sh", ".github/workflows/proof-check-selftest.yml"):
        assert pattern.search((REPO_ROOT / relative).read_text()) is None
