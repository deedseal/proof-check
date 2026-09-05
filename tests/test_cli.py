"""CLI exits, credential handling, and network-disabled offline verification."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import proof_check.cli as cli_module
import proof_check.contracts as contract_module
from proof_check.canonical import canonical_bytes
from proof_check.cli import main
from proof_check.github_read import HTTPResponse
from proof_check.verify import receipt_digest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "a" * 40
HEAD = "1" * 40
BASE = "2" * 40
SHA_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")


def _policy(entries: list[str], *, indeterminate_exit: int = 20) -> dict:
    return {
        "schema_version": "proof-check-policy/v1",
        "scope": {"source": "pinned:" + "0" * 64, "trust": "blocking", "entries": entries},
        "checks": {"required_selectors": []},
        "reviews": {"policy": "report_only"},
        "verdict_policy": {"fail_exit": 10, "indeterminate_exit": indeterminate_exit},
        "receipt": {"mode": "always"},
    }


class CLIFixtureTransport:
    def __init__(
        self,
        *,
        changed_files: int = 1,
        paths: list[str] | None = None,
        moved_sha: str | None = None,
        merge_sha: str | None = "3" * 40,
    ):
        self.changed_files = changed_files
        self.paths = paths if paths is not None else ["docs/readme.md"]
        self.moved_sha = moved_sha
        self.merge_sha = merge_sha
        self.pull_reads = 0
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def response(value, status: int = 200):
        return HTTPResponse(status, {}, json.dumps(value).encode("utf-8"))

    def request(self, method, url, headers):
        self.calls.append((method, url))
        parsed = urlsplit(url)
        if parsed.path.endswith("/pulls/9"):
            self.pull_reads += 1
            sha = self.moved_sha if self.moved_sha and self.pull_reads > 1 else HEAD
            return self.response(
                {
                    "state": "open",
                    "draft": False,
                    "changed_files": self.changed_files,
                    "merge_commit_sha": self.merge_sha,
                    "body": "## Allowlist\n- docs/**\n",
                    "user": {"login": "octo-author"},
                    "base": {"ref": "main", "sha": BASE, "repo": {"id": 9, "private": False}},
                    "head": {"ref": "feature/docs", "sha": sha},
                }
            )
        if parsed.path.endswith("/pulls/9/files"):
            page = int(parse_qs(parsed.query).get("page", ["1"])[0])
            return self.response([{"filename": path, "status": "modified"} for path in self.paths] if page == 1 else [])
        if "/check-runs" in parsed.path:
            return self.response({"total_count": 0, "check_runs": []})
        if parsed.path.endswith("/pulls/9/reviews"):
            return self.response([])
        return self.response({}, 404)


def _run_check(tmp_path: Path, monkeypatch, policy: dict, transport: CLIFixtureTransport):
    policy_path = tmp_path / "policy.json"
    receipt_path = tmp_path / "receipt.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "credential-sentinel")
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        [
            "check",
            "--repo",
            "example-org/example-repo",
            "--pr",
            "9",
            "--policy",
            str(policy_path),
            "--receipt",
            str(receipt_path),
        ],
        transport=transport,
        source_commit=SOURCE_COMMIT,
        stdout=stdout,
        stderr=stderr,
    )
    return code, receipt_path, stdout.getvalue(), stderr.getvalue()


def _copy_gitless_source(destination: Path) -> None:
    shutil.copytree(
        REPO_ROOT,
        destination,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc"),
    )


def _init_unrelated_repository(path: Path, *, ignore_venv: bool = False) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Proof Check test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "proof-check@example.invalid"], cwd=path, check=True)
    (path / "unrelated.txt").write_text("unrelated repository\n", encoding="utf-8")
    if ignore_venv:
        (path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "unrelated"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _install_source(source: Path, venv: Path) -> Path:
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], check=True)
    python = venv / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "--no-build-isolation", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    return python


def _assert_installed_source_is_refused(python: Path, run_directory: Path, unrelated_sha: str, tmp_path: Path) -> None:
    resolve = subprocess.run(
        [
            str(python),
            "-c",
            "from proof_check.cli import resolve_source_commit\n"
            "try:\n resolve_source_commit()\n"
            "except ValueError:\n print('RAISED')\n"
            "else:\n print('DID_NOT_RAISE')\n",
        ],
        cwd=run_directory,
        capture_output=True,
        text=True,
        check=True,
    )
    assert resolve.stdout.strip() == "RAISED"
    assert unrelated_sha not in resolve.stdout + resolve.stderr
    assert SHA_RE.search(resolve.stdout + resolve.stderr) is None

    policy_path = tmp_path / "policy.json"
    receipt_path = tmp_path / "receipt.json"
    policy_path.write_text(json.dumps(_policy(["docs/**"])), encoding="utf-8")
    check_script = (
        "import sys\n"
        "from proof_check.cli import main\n"
        "class BombTransport:\n"
        " def request(self, method, url, headers):\n"
        "  raise AssertionError('network call was not expected')\n"
        "raise SystemExit(main(sys.argv[1:], transport=BombTransport()))\n"
    )
    environment = dict(os.environ)
    environment["GITHUB_TOKEN"] = "credential-sentinel"
    checked = subprocess.run(
        [
            str(python),
            "-c",
            check_script,
            "check",
            "--repo",
            "example-org/example-repo",
            "--pr",
            "9",
            "--policy",
            str(policy_path),
            "--receipt",
            str(receipt_path),
        ],
        cwd=run_directory,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 2
    assert not receipt_path.exists()
    assert unrelated_sha not in checked.stdout + checked.stderr
    assert SHA_RE.search(checked.stdout + checked.stderr) is None


def test_untracked_gitless_source_cannot_borrow_an_unrelated_repository_head(tmp_path):
    unrelated = tmp_path / "unrelated"
    unrelated_sha = _init_unrelated_repository(unrelated)
    source = unrelated / "proof-check-src"
    _copy_gitless_source(source)
    python = _install_source(source, tmp_path / "venv")
    _assert_installed_source_is_refused(python, tmp_path, unrelated_sha, tmp_path)


def test_gitignored_venv_cannot_borrow_an_unrelated_repository_head(tmp_path):
    source = tmp_path / "proof-check-src"
    _copy_gitless_source(source)
    unrelated = tmp_path / "unrelated"
    unrelated_sha = _init_unrelated_repository(unrelated, ignore_venv=True)
    python = _install_source(source, unrelated / ".venv")
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=unrelated, check=True, capture_output=True, text=True
    ).stdout == ""
    _assert_installed_source_is_refused(python, tmp_path, unrelated_sha, tmp_path)


def test_pristine_clone_records_its_known_commit(tmp_path):
    clone = tmp_path / "proof-check-clone"
    subprocess.run(["git", "clone", "--quiet", "--no-local", str(REPO_ROOT), str(clone)], check=True)
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, check=True, capture_output=True, text=True
    ).stdout.strip()
    python = _install_source(clone, tmp_path / "venv")
    resolved = subprocess.run(
        [str(python), "-c", "from proof_check.cli import resolve_source_commit; print(resolve_source_commit())"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert resolved.returncode == 0
    assert resolved.stderr == ""
    assert resolved.stdout.strip() == expected


def test_check_exit_matrix_and_receipt_output(tmp_path, monkeypatch):
    passing = _run_check(tmp_path, monkeypatch, _policy(["docs/**"]), CLIFixtureTransport())
    assert passing[0] == 0
    assert json.loads(passing[1].read_bytes())["verdict"] == "PASS"

    failing_dir = tmp_path / "fail"
    failing_dir.mkdir()
    failing = _run_check(failing_dir, monkeypatch, _policy(["README.md"]), CLIFixtureTransport())
    assert failing[0] == 10
    assert json.loads(failing[1].read_bytes())["verdict"] == "FAIL"

    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    missing = _run_check(
        missing_dir,
        monkeypatch,
        _policy(["docs/**"]),
        CLIFixtureTransport(changed_files=2, paths=["docs/readme.md"]),
    )
    assert missing[0] == 20
    receipt = json.loads(missing[1].read_bytes())
    assert receipt["verdict"] == "INDETERMINATE"
    assert receipt["reason_codes"] == ["EVIDENCE_MISSING"]


def test_token_bytes_do_not_enter_receipt_output_or_log(tmp_path, monkeypatch):
    code, receipt_path, stdout, stderr = _run_check(
        tmp_path, monkeypatch, _policy(["docs/**"]), CLIFixtureTransport()
    )
    log_path = tmp_path / "check.log"
    log_path.write_text(stdout + stderr, encoding="utf-8")
    assert code == 0
    for content in (receipt_path.read_text(encoding="utf-8"), stdout, stderr, log_path.read_text(encoding="utf-8")):
        assert "credential-sentinel" not in content


def test_invalid_inputs_exit_two_without_network(tmp_path, monkeypatch):
    class BombTransport:
        def request(self, method, url, headers):
            raise AssertionError("network call was not expected")

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        ["check", "--repo", "example-org/example-repo", "--pr", "9", "--policy", str(tmp_path / "absent.json"), "--receipt", str(tmp_path / "out.json")],
        transport=BombTransport(),
        source_commit=SOURCE_COMMIT,
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 2 and "unreadable" in stderr.getvalue()

    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(_policy(["docs/**"])), encoding="utf-8")
    stderr = io.StringIO()
    code = main(
        ["check", "--repo", "example-org/example-repo", "--pr", "9", "--policy", str(policy_path), "--receipt", str(tmp_path / "out.json"), "--target", "merge_group"],
        transport=BombTransport(),
        source_commit=SOURCE_COMMIT,
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert code == 2 and "event context" in stderr.getvalue()

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    stderr = io.StringIO()
    code = main(
        ["check", "--repo", "example-org/example-repo", "--pr", "9", "--policy", str(policy_path), "--receipt", str(tmp_path / "out.json")],
        transport=BombTransport(),
        source_commit=SOURCE_COMMIT,
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert code == 2 and "GITHUB_TOKEN" in stderr.getvalue()


def test_receipt_directory_is_refused_before_observation(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(_policy(["docs/**"])), encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "credential-sentinel")
    transport = CLIFixtureTransport()
    stderr = io.StringIO()
    code = main(
        [
            "check",
            "--repo",
            "example-org/example-repo",
            "--pr",
            "9",
            "--policy",
            str(policy_path),
            "--receipt",
            str(tmp_path / "missing" / "receipt.json"),
        ],
        transport=transport,
        source_commit=SOURCE_COMMIT,
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert "receipt directory does not exist" in stderr.getvalue()
    assert transport.calls == []


def test_crlf_declaration_produces_and_verifies_the_same_receipt(tmp_path, monkeypatch):
    declaration_bytes = b"docs/**\r\n"
    policy = _policy(["docs/**"])
    del policy["scope"]["entries"]
    policy["scope"]["source"] = "pinned:" + hashlib.sha256(declaration_bytes).hexdigest()
    policy_path = tmp_path / "policy.json"
    declaration_path = tmp_path / "declaration.txt"
    receipt_path = tmp_path / "receipt.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    declaration_path.write_bytes(declaration_bytes)
    monkeypatch.setenv("GITHUB_TOKEN", "credential-sentinel")
    check_stdout = io.StringIO()
    assert main(
        [
            "check",
            "--repo",
            "example-org/example-repo",
            "--pr",
            "9",
            "--policy",
            str(policy_path),
            "--receipt",
            str(receipt_path),
            "--declaration",
            str(declaration_path),
        ],
        transport=CLIFixtureTransport(),
        source_commit=SOURCE_COMMIT,
        stdout=check_stdout,
        stderr=io.StringIO(),
    ) == 0
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["policy"]["raw_sha256"] == hashlib.sha256(declaration_bytes).hexdigest()

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    (bundle / "declaration.txt").write_bytes(declaration_bytes)
    verify_stdout = io.StringIO()
    assert main(
        ["verify", str(receipt_path), "--offline-bundle", str(bundle)],
        stdout=verify_stdout,
        stderr=io.StringIO(),
    ) == 0
    assert "VERIFIED" in verify_stdout.getvalue()


def test_unknown_policy_and_missing_schemas_exit_two_without_traceback(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"schema_version": "proof-check-policy/future"}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "credential-sentinel")
    monkeypatch.setattr(cli_module, "_direct_url_document", lambda: None)

    def missing_schema_directory():
        raise ValueError("schemas directory is missing")

    contract_module.load_schema.cache_clear()
    contract_module._validator.cache_clear()
    monkeypatch.setattr(contract_module, "_schema_dir", missing_schema_directory)
    stderr = io.StringIO()
    transport = CLIFixtureTransport()
    try:
        code = main(
            [
                "check",
                "--repo",
                "example-org/example-repo",
                "--pr",
                "9",
                "--policy",
                str(policy_path),
                "--receipt",
                str(tmp_path / "receipt.json"),
            ],
            transport=transport,
            source_commit=SOURCE_COMMIT,
            stdout=io.StringIO(),
            stderr=stderr,
        )
    finally:
        contract_module.load_schema.cache_clear()
        contract_module._validator.cache_clear()
    assert code == 2
    assert "schemas directory is missing" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()
    assert transport.calls == []


@pytest.mark.parametrize("endpoint", ["check-runs", "reviews"])
def test_secondary_observation_failures_print_their_error_codes(tmp_path, monkeypatch, endpoint):
    class SecondaryFailureTransport(CLIFixtureTransport):
        def request(self, method, url, headers):
            parsed = urlsplit(url)
            if endpoint in parsed.path:
                self.calls.append((method, url))
                return self.response({}, 403)
            return super().request(method, url, headers)

    code, _receipt_path, stdout, _stderr = _run_check(
        tmp_path, monkeypatch, _policy(["docs/**"]), SecondaryFailureTransport()
    )
    assert code == 20
    assert "OBSERVATION_ERRORS: GITHUB_RATE_LIMIT_OR_FORBIDDEN" in stdout


def test_indeterminate_exit_zero_prints_the_explicit_policy_opt_out(tmp_path, monkeypatch):
    code, receipt_path, stdout, stderr = _run_check(
        tmp_path,
        monkeypatch,
        _policy(["docs/**"], indeterminate_exit=0),
        CLIFixtureTransport(changed_files=2, paths=["docs/readme.md"]),
    )
    assert code == 0
    assert json.loads(receipt_path.read_bytes())["verdict"] == "INDETERMINATE"
    assert "EXIT 0 BY POLICY OPT-OUT (verdict_policy.indeterminate_exit: 0)" in stdout
    assert stderr == ""


def test_multiline_token_is_refused_without_observation(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(_policy(["docs/**"])), encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "credential-sentinel\nsecond-line")
    transport = CLIFixtureTransport()
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        [
            "check",
            "--repo",
            "example-org/example-repo",
            "--pr",
            "9",
            "--policy",
            str(policy_path),
            "--receipt",
            str(tmp_path / "receipt.json"),
        ],
        transport=transport,
        source_commit=SOURCE_COMMIT,
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 2
    assert transport.calls == []
    assert "must contain one line" in stderr.getvalue()
    assert "credential-sentinel" not in stdout.getvalue() + stderr.getvalue()


def test_malformed_receipt_exits_two_without_network(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{not json", encoding="utf-8")

    class BombTransport:
        def request(self, method, url, headers):
            raise AssertionError("network call was not expected")

    code = main(["verify", str(receipt)], transport=BombTransport(), stdout=io.StringIO(), stderr=io.StringIO())
    assert code == 2


def test_offline_example_uses_no_transport():
    class BombTransport:
        def request(self, method, url, headers):
            raise AssertionError("offline verification attempted a network call")

    bundle = REPO_ROOT / "examples" / "offline-bundle"
    stdout = io.StringIO()
    assert main(
        ["verify", str(bundle / "receipt.json"), "--offline-bundle", str(bundle)],
        transport=BombTransport(),
        stdout=stdout,
        stderr=io.StringIO(),
    ) == 0
    assert "VERIFIED" in stdout.getvalue()

    stdout = io.StringIO()
    assert main(
        ["verify", str(bundle / "receipt-tampered.json"), "--offline-bundle", str(bundle)],
        transport=BombTransport(),
        stdout=stdout,
        stderr=io.StringIO(),
    ) == 20
    assert "UNVERIFIABLE" in stdout.getvalue()


def test_offline_bundle_rejects_widened_redigested_allowed_entries(tmp_path):
    bundle = REPO_ROOT / "examples" / "offline-bundle"
    receipt = json.loads((bundle / "receipt.json").read_bytes())
    receipt["policy"]["allowed_entries"].append("src/**")
    receipt["receipt_digest"] = receipt_digest(receipt)
    forged = tmp_path / "receipt-forged.json"
    forged.write_bytes(canonical_bytes(receipt))
    stdout = io.StringIO()
    code = main(
        ["verify", str(forged), "--offline-bundle", str(bundle)],
        stdout=stdout,
        stderr=io.StringIO(),
    )
    assert code == 20
    assert "allowed_entries do not match" in stdout.getvalue()


def test_online_verify_reports_differences_without_changing_exit(tmp_path, monkeypatch):
    code, receipt_path, _stdout, _stderr = _run_check(
        tmp_path, monkeypatch, _policy(["docs/**"]), CLIFixtureTransport()
    )
    assert code == 0
    original_receipt = receipt_path.read_bytes()
    writes = 0
    original_write_bytes = Path.write_bytes

    def write_bytes_probe(path, data):
        nonlocal writes
        writes += 1
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes_probe)
    stdout = io.StringIO()
    code = main(
        ["verify", str(receipt_path)],
        transport=CLIFixtureTransport(moved_sha="4" * 40),
        stdout=stdout,
        stderr=io.StringIO(),
    )
    assert code == 0
    output = stdout.getvalue()
    assert "recorded verdict unchanged" in output
    assert "HEAD_MOVED" in output
    assert "- COORDINATE_DRIFT:" in output
    assert writes == 0
    assert receipt_path.read_bytes() == original_receipt


def test_unexpected_transport_error_is_redacted(tmp_path, monkeypatch):
    class FailingTransport:
        def request(self, method, url, headers):
            raise RuntimeError("credential-sentinel")

    code, receipt_path, stdout, stderr = _run_check(
        tmp_path, monkeypatch, _policy(["docs/**"]), FailingTransport()
    )
    assert code == 20
    assert not receipt_path.exists()
    assert "credential-sentinel" not in stdout + stderr


def test_generic_handler_does_not_render_the_exception(tmp_path, monkeypatch):
    class ProbeError(Exception):
        def __init__(self):
            self.render_count = 0

        def __str__(self):
            self.render_count += 1
            return "credential-sentinel"

    error = ProbeError()

    def fail_collection(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli_module, "collect_evidence", fail_collection)
    code, receipt_path, stdout, stderr = _run_check(
        tmp_path, monkeypatch, _policy(["docs/**"]), CLIFixtureTransport()
    )
    assert code == 20
    assert error.render_count == 0
    assert not receipt_path.exists()
    assert "credential-sentinel" not in stdout + stderr


def test_missing_test_merge_coordinate_is_indeterminate(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.json"
    receipt_path = tmp_path / "receipt.json"
    policy_path.write_text(json.dumps(_policy(["docs/**"])), encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "credential-sentinel")
    stdout = io.StringIO()
    code = main(
        [
            "check",
            "--repo",
            "example-org/example-repo",
            "--pr",
            "9",
            "--policy",
            str(policy_path),
            "--receipt",
            str(receipt_path),
            "--target",
            "test_merge",
        ],
        transport=CLIFixtureTransport(merge_sha=None),
        source_commit=SOURCE_COMMIT,
        stdout=stdout,
        stderr=io.StringIO(),
    )
    assert code == 20
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["verdict"] == "INDETERMINATE"
    assert "EVIDENCE_MISSING" in receipt["reason_codes"]
    assert "OBSERVATION_ERRORS: TEST_MERGE_SHA_MISSING" in stdout.getvalue()
