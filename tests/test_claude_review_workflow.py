"""Workflow invariants and offline probes of the actual inline record program."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / '.github/workflows/claude-review.yml').read_text()
PROMPT = (ROOT / '.github/claude-review/PROMPT.md').read_text()
HEAD = 'a' * 40
RUN = '1234'
START = '2026-09-08T12:00:00Z'
MARKER = f'CLAUDE_REVIEW/v1 head={HEAD} run={RUN} verdict=CLEAN findings=0'


def step(name):
    # Extract literal YAML blocks without adding a project runtime dependency.
    match = re.search(
        rf'^      - name: {re.escape(name)}\n(.*?)(?=^      - name: |\Z)',
        WORKFLOW, re.MULTILINE | re.DOTALL,
    )
    assert match, name
    return match.group(1)


def program(name):
    block = step(name)
    match = re.search(r"          python3 -I - <<'PY'\n(.*?)^          PY$",
                      block, re.MULTILINE | re.DOTALL)
    assert match, name
    return textwrap.dedent(match.group(1))


def test_trigger_permissions_and_draft_guard_are_preserved():
    assert 'on:\n  pull_request:\n    types: [opened, ready_for_review]\n' in WORKFLOW
    assert 'permissions:\n  contents: read\n  pull-requests: read\n  issues: read\n  id-token: write\n' in WORKFLOW
    assert 'github.event.pull_request.draft == false &&' in WORKFLOW
    assert 'github.event.pull_request.head.repo.full_name == github.repository' in WORKFLOW
    assert 'timeout-minutes: 30' in WORKFLOW
    assert 'cancel-in-progress: true' in WORKFLOW
    assert 'continue-on-error:' not in WORKFLOW


def test_pins_model_and_prompt_input():
    uses = re.findall(r'^\s+uses: (\S+)', WORKFLOW, re.MULTILINE)
    assert uses == [
        'actions/checkout@11d5960a326750d5838078e36cf38b85af677262',
        'actions/checkout@11d5960a326750d5838078e36cf38b85af677262',
        'anthropics/claude-code-action@9c5ddab2e6d17b83ea679153b31f1d5f023cf636',
        'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',
        'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',
        'actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349',
        'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',
    ]
    review = step('Review exact PR head')
    assert '--model claude-sonnet-5' in review
    assert '--max-turns 16' in review
    assert 'prompt: ${{ steps.prompt.outputs.prompt }}' in review
    for absent in ('plugin_marketplaces:', 'plugins:', 'prompt_file:', '/code-review:'):
        assert absent not in WORKFLOW
    for flag in ('include_fix_links', 'show_full_output', 'display_report'):
        assert f'{flag}: false' in review
    checkout = step('Checkout exact PR head as review subject')
    assert 'ref: ${{ github.event.pull_request.head.sha }}' in checkout
    assert 'persist-credentials: false' in checkout


def test_reviewer_has_only_bounded_gh_tools():
    review = step('Review exact PR head')
    claude_args = review.split('          claude_args: |\n', 1)[1]
    assert 'gh api' not in claude_args
    allowed_lines = [line.strip() for line in claude_args.splitlines()
                     if line.strip().startswith('--allowedTools ')]
    assert allowed_lines == [
        '--allowedTools "Read,Glob,Grep,Bash(gh pr view:*),Bash(gh pr comment:*),'
        'Bash(gh pr diff:*),Bash(git diff:*),Bash(git log:*),Bash(git show:*),'
        'Bash(git status:*),LS,mcp__github_inline_comment__create_inline_comment"'
    ]
    for write_tool in ('Write', 'Edit', 'NotebookEdit', 'Bash(git add:', 'Bash(git commit:',
                       'Bash(git push:', 'Bash(gh api:'):
        assert write_tool not in claude_args
    assert 'gh api' not in PROMPT


def test_prompt_is_loaded_only_from_trusted_main_checkout():
    checkout = step('Checkout trusted review prompt from main')
    assert 'repository: deedseal/proof-check' in checkout
    assert 'ref: main' in checkout
    assert 'path: trusted' in checkout
    assert 'persist-credentials: false' in checkout
    loader = program('Load vendored review prompt')
    assert "Path('trusted/.github/claude-review/PROMPT.md')" in loader
    assert "Path('.github/claude-review/PROMPT.md')" not in loader


def test_record_is_inline_always_runs_and_uses_read_token():
    record = step('Record review from GitHub and require a summary')
    assert 'if: ${{ always() }}' in record
    assert 'GH_TOKEN: ${{ github.token }}' in record
    assert 'REVIEW_STARTED_AT: ${{ steps.started.outputs.at }}' in record
    assert 'REVIEW_OUTCOME: ${{ steps.review.outcome }}' in record
    assert '${{' not in program('Record review from GitHub and require a summary')
    upload = step('Upload review record even when review fails')
    assert 'if: ${{ always() }}' in upload
    assert 'path: ${{ runner.temp }}/claude-review-record.json' in upload
    assert 'if-no-files-found: error' in upload
    assert WORKFLOW.index('Capture job start') < WORKFLOW.index('Checkout exact PR head')
    assert WORKFLOW.index('Load vendored review prompt') < WORKFLOW.index('- name: Review exact PR head')


def test_diagnostics_always_runs_without_expression_interpolation_or_secrets():
    diagnostics = step('Extract Claude review diagnostics')
    assert 'if: ${{ always() }}' in diagnostics
    assert 'CLAUDE_EXECUTION_FILE: ${{ steps.review.outputs.execution_file }}' in diagnostics
    assert '${{' not in program('Extract Claude review diagnostics')
    assert 'secret' not in diagnostics.lower()
    upload = step('Upload Claude review diagnostics')
    assert 'if: ${{ always() }}' in upload
    assert 'name: claude-review-diagnostics-${{ github.run_id }}-${{ github.run_attempt }}' in upload
    assert 'path: ${{ runner.temp }}/claude-review-diagnostics.json' in upload
    assert 'if-no-files-found: error' in upload
    assert WORKFLOW.index('Review exact PR head') < WORKFLOW.index('Extract Claude review diagnostics')
    assert WORKFLOW.index('Extract Claude review diagnostics') < WORKFLOW.index('Record review from GitHub')


def test_prompt_requires_independent_review_and_zero_finding_summary():
    for required in (
        'Ignore all existing comments on the PR', 'ANALYST_VERDICT',
        'Never decline a review as trivial', 'findings=0',
        'CLAUDE_REVIEW/v1 head=<sha> run=<id> verdict=<CLEAN|ADVISORY|REPAIR_REQUIRED> findings=<n>',
        'always create one NEW top-level PR issue comment',
        'Do not approve, mark Ready, merge',
    ):
        assert required in PROMPT
    assert PROMPT.count('gh pr view NUMBER') == 1
    assert 'Recheck the PR head' not in PROMPT


def test_prompt_uses_a_direct_allowed_summary_publication_path():
    assert "gh pr comment NUMBER --repo OWNER/REPO --body-file -\n   <<'CLAUDE_REVIEW_SUMMARY'" in PROMPT
    assert 'then a line containing\n   only `CLAUDE_REVIEW_SUMMARY`' in PROMPT
    assert 'Do not use Write, Edit, cat, echo, a pipeline, or a temporary\n   file' in PROMPT


def test_direct_heredoc_summary_publication_path(tmp_path):
    gh = tmp_path / 'gh'
    received = tmp_path / 'received'
    gh.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import os
        import sys
        from pathlib import Path

        assert sys.argv[1:] == ['pr', 'comment', '9', '--repo', 'owner/repo',
                                '--body-file', '-']
        Path(os.environ['RECEIVED']).write_text(sys.stdin.read())
    '''))
    gh.chmod(0o755)
    summary = f'Reviewed fixture.\n\nCLAUDE_REVIEW head={HEAD} run={RUN} findings=0\n'
    command = """gh pr comment 9 --repo owner/repo --body-file - <<'CLAUDE_REVIEW_SUMMARY'
{summary}CLAUDE_REVIEW_SUMMARY
""".format(summary=summary)
    result = subprocess.run(
        ['bash', '-c', command], text=True, capture_output=True,
        env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
             'RECEIVED': str(received)},
    )
    assert result.returncode == 0, result.stderr
    assert received.read_text() == summary


def test_loads_file_bytes_and_exact_context(tmp_path):
    trusted_prompt = tmp_path / 'trusted/.github/claude-review/PROMPT.md'
    trusted_prompt.parent.mkdir(parents=True)
    trusted_prompt.write_text(PROMPT)
    output = tmp_path / 'output'
    result = subprocess.run(
        ['python3', '-I', '-'], input=program('Load vendored review prompt'),
        cwd=tmp_path, text=True, capture_output=True,
        env={**os.environ, 'GITHUB_OUTPUT': str(output), 'GITHUB_REPOSITORY': 'owner/repo',
             'REVIEW_PR': '9', 'REVIEW_HEAD': HEAD, 'GITHUB_RUN_ID': RUN},
    )
    assert result.returncode == 0, result.stderr
    content = output.read_text()
    assert PROMPT in content
    assert f'Exact head: {HEAD}\nRun ID: {RUN}\n' in content
    assert 'Repository: owner/repo\nPR number: 9\n' in content
    assert content.splitlines()[0].split('<<')[1] == content.splitlines()[-1]


def run_diagnostics(tmp_path, execution):
    execution_file = tmp_path / 'execution.json'
    if execution is not None:
        if isinstance(execution, str):
            execution_file.write_text(execution)
        else:
            execution_file.write_text(json.dumps(execution))
    summary = tmp_path / 'summary.md'
    result = subprocess.run(
        ['python3', '-I', '-'], input=program('Extract Claude review diagnostics'),
        cwd=tmp_path, text=True, capture_output=True,
        env={**os.environ, 'CLAUDE_EXECUTION_FILE': str(execution_file),
             'RUNNER_TEMP': str(tmp_path), 'GITHUB_STEP_SUMMARY': str(summary)},
    )
    artifact_bytes = (tmp_path / 'claude-review-diagnostics.json').read_text()
    assert artifact_bytes in summary.read_text()
    diagnostics = json.loads(artifact_bytes)
    assert set(diagnostics) <= {
        'available', 'reason', 'num_turns', 'duration_ms', 'is_error', 'subtype',
        'denials',
    }
    if 'reason' in diagnostics:
        assert diagnostics['available'] is False
        assert diagnostics['reason'] in {
            'NO_FILE', 'UNPARSEABLE', 'NO_RESULT_MESSAGE',
            'INVALID_FIELD', 'INVALID_DENIAL',
        }
    for denial in diagnostics.get('denials', []):
        assert set(denial) == {'tool_name', 'count'}
    return result, diagnostics, artifact_bytes + summary.read_text()


def test_diagnostics_extracts_normal_execution_file(tmp_path):
    execution = [
        {'type': 'system', 'subtype': 'init', 'message': 'PRIVATE_MESSAGE_SENTINEL'},
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'duration_ms': 4321, 'num_turns': 7, 'permission_denials': [],
         'total_cost_usd': 1.25, 'modelUsage': {'model': {'inputTokens': 98765}}},
    ]
    result, diagnostics, emitted = run_diagnostics(tmp_path, execution)
    assert result.returncode == 0, result.stderr
    assert diagnostics == {
        'duration_ms': 4321, 'is_error': False, 'num_turns': 7,
        'subtype': 'success', 'denials': [],
    }
    for forbidden in ('PRIVATE_MESSAGE_SENTINEL', 'total_cost_usd', 'modelUsage',
                      'inputTokens', '98765', '1.25'):
        assert forbidden not in emitted


def test_diagnostics_aggregates_denials_without_tool_inputs(tmp_path):
    execution = [
        {'type': 'assistant', 'message': {'content': 'PRIVATE_PROMPT_SENTINEL'}},
        {'type': 'result', 'subtype': 'error_max_turns', 'is_error': True,
         'duration_ms': 53968, 'num_turns': 17, 'permission_denials': [
             {'tool_name': 'Bash', 'tool_use_id': 'PRIVATE_ID_SENTINEL',
              'tool_input': {'command': 'cat PRIVATE_FILE_SENTINEL'}},
             {'tool_name': 'LS', 'tool_use_id': 'another-id',
              'tool_input': {'path': 'PRIVATE_PATH_SENTINEL'}},
             {'tool_name': 'Bash', 'tool_use_id': 'third-id',
              'tool_input': {'command': 'git diff PRIVATE_DIFF_SENTINEL'}},
         ]},
    ]
    result, diagnostics, emitted = run_diagnostics(tmp_path, execution)
    assert result.returncode == 0, result.stderr
    assert diagnostics == {
        'duration_ms': 53968, 'is_error': True, 'num_turns': 17,
        'subtype': 'error_max_turns',
        'denials': [{'count': 2, 'tool_name': 'Bash'},
                    {'count': 1, 'tool_name': 'LS'}],
    }
    for forbidden in ('PRIVATE_PROMPT_SENTINEL', 'PRIVATE_ID_SENTINEL',
                      'PRIVATE_FILE_SENTINEL', 'PRIVATE_PATH_SENTINEL',
                      'PRIVATE_DIFF_SENTINEL', 'tool_use_id', 'tool_input'):
        assert forbidden not in emitted


def test_diagnostics_treats_absent_permission_denials_as_empty(tmp_path):
    execution = [
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'duration_ms': 50, 'num_turns': 2},
    ]
    result, diagnostics, _ = run_diagnostics(tmp_path, execution)
    assert result.returncode == 0, result.stderr
    assert diagnostics == {
        'duration_ms': 50, 'is_error': False, 'num_turns': 2,
        'subtype': 'success', 'denials': [],
    }


def test_diagnostics_reports_no_file(tmp_path):
    result, diagnostics, _ = run_diagnostics(tmp_path, None)
    assert result.returncode == 0, result.stderr
    assert diagnostics == {'available': False, 'reason': 'NO_FILE'}


def test_diagnostics_reports_unparseable_file(tmp_path):
    result, diagnostics, _ = run_diagnostics(tmp_path, '{not json')
    assert result.returncode == 0, result.stderr
    assert diagnostics == {'available': False, 'reason': 'UNPARSEABLE'}


def test_diagnostics_reports_missing_result_message(tmp_path):
    execution = [{'type': 'system', 'subtype': 'init'}]
    result, diagnostics, _ = run_diagnostics(tmp_path, execution)
    assert result.returncode == 0, result.stderr
    assert diagnostics == {'available': False, 'reason': 'NO_RESULT_MESSAGE'}


def test_diagnostics_degrades_invalid_scalar_field(tmp_path):
    execution = [
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'duration_ms': 75, 'num_turns': 'PRIVATE_INVALID_FIELD_SENTINEL',
         'permission_denials': []},
    ]
    result, diagnostics, emitted = run_diagnostics(tmp_path, execution)
    assert result.returncode == 0, result.stderr
    assert diagnostics == {
        'available': False, 'reason': 'INVALID_FIELD',
        'duration_ms': 75, 'is_error': False, 'num_turns': None,
        'subtype': 'success', 'denials': [],
    }
    assert 'PRIVATE_INVALID_FIELD_SENTINEL' not in emitted


def test_diagnostics_buckets_invalid_denial(tmp_path):
    execution = [
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'duration_ms': 100, 'num_turns': 3, 'permission_denials': [
             {'tool_name': 'Bash'},
             {'tool_name': 'PRIVATE_INVALID_DENIAL_SENTINEL\n```'},
             {'tool_input': {'command': 'PRIVATE_COMMAND_SENTINEL'}},
         ]},
    ]
    result, diagnostics, emitted = run_diagnostics(tmp_path, execution)
    assert result.returncode == 0, result.stderr
    assert diagnostics == {
        'available': False, 'reason': 'INVALID_DENIAL',
        'duration_ms': 100, 'is_error': False, 'num_turns': 3,
        'subtype': 'success',
        'denials': [{'count': 2, 'tool_name': '<invalid>'},
                    {'count': 1, 'tool_name': 'Bash'}],
    }
    assert 'PRIVATE_INVALID_DENIAL_SENTINEL' not in emitted
    assert 'PRIVATE_COMMAND_SENTINEL' not in emitted


def comment(**changes):
    return {
        'id': 71, 'user': {'id': 41898282, 'login': 'claude[bot]'},
        'created_at': '2026-09-08T12:00:01Z',
        'commit_id': HEAD, 'body': MARKER, **changes,
    }


def run_record(tmp_path, *, inline=None, summaries=None, conclusion='success',
               outcome='success', api_error=False, malformed=False, started=START):
    data = tmp_path / 'data.json'
    data.write_text(json.dumps({'inline': inline or [], 'summaries': summaries or [],
                                'api_error': api_error, 'malformed': malformed}))
    gh = tmp_path / 'gh'
    gh.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json, os, sys
        from pathlib import Path
        from urllib.parse import parse_qs, urlsplit
        assert sys.argv[1] == 'api' and len(sys.argv) == 3
        endpoint = urlsplit(sys.argv[2])
        assert endpoint.path in ('repos/owner/repo/pulls/9/comments',
                                 'repos/owner/repo/issues/9/comments')
        with open(os.environ['CALLS'], 'a') as calls:
            calls.write(sys.argv[2] + '\\n')
        data = json.loads(Path(os.environ['DATA']).read_text())
        if data['api_error']:
            sys.stderr.write('private API error details')
            sys.exit(1)
        if data['malformed']:
            print('{}')
            sys.exit(0)
        rows = data['inline' if '/pulls/' in endpoint.path else 'summaries']
        query = parse_qs(endpoint.query)
        assert query['per_page'] == ['100']
        page = int(query['page'][0])
        print(json.dumps(rows[(page - 1) * 100:page * 100]))
    '''))
    gh.chmod(0o755)
    summary = tmp_path / 'summary.md'
    calls = tmp_path / 'calls'
    result = subprocess.run(
        ['python3', '-I', '-'], input=program('Record review from GitHub and require a summary'),
        cwd=tmp_path, text=True, capture_output=True,
        env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
             'DATA': str(data), 'CALLS': str(calls), 'RUNNER_TEMP': str(tmp_path),
             'GITHUB_STEP_SUMMARY': str(summary), 'REVIEW_REPOSITORY': 'owner/repo',
             'REVIEW_PR': '9', 'REVIEW_HEAD': HEAD, 'REVIEW_RUN_ID': RUN,
             'GITHUB_OUTPUT': str(tmp_path / 'output'), 'REVIEW_STARTED_AT': started, 'REVIEW_CONCLUSION': conclusion,
             'REVIEW_OUTCOME': outcome},
    )
    record_bytes = (tmp_path / 'claude-review-record.json').read_text()
    assert record_bytes in summary.read_text()
    record = json.loads(record_bytes)
    assert set(record) == {'head_sha', 'run_id', 'conclusion', 'inline_comments', 'summary_comments',
                           'summary_comment_id', 'summary_sha256'}
    assert record['head_sha'] == HEAD and record['run_id'] == RUN
    return result, record, calls


def test_zero_findings_summary_is_sufficient(tmp_path):
    result, record, _ = run_record(tmp_path, summaries=[comment()])
    assert result.returncode == 0, result.stderr
    assert record['conclusion'] == 'success'
    assert (record['inline_comments'], record['summary_comments']) == (0, 1)


@pytest.mark.parametrize('changes', [
    {'user': {'id': 1, 'login': 'claude[bot]'}},
    {'user': {'id': 41898282, 'login': 'someone-else'}},
    {'created_at': START},
    {'created_at': '2026-09-07T12:00:00Z'},
    {'created_at': '2026-09-08T14:00:00+02:00'},
    {'body': 'Review starting now'},
    {'body': MARKER.replace(HEAD, 'b' * 40)},
    {'body': MARKER.replace(RUN, '9999')},
    {'body': MARKER.replace('findings=0', 'findings=many')},
    {'body': None},
])
def test_unrelated_comments_cannot_satisfy_summary(tmp_path, changes):
    result, record, _ = run_record(tmp_path, inline=[comment()], summaries=[comment(**changes)])
    assert result.returncode == 1
    assert record['conclusion'] == 'failure'
    assert (record['inline_comments'], record['summary_comments']) == (1, 0)


def test_no_comments_fails_even_if_reviewer_reports_success(tmp_path):
    result, record, _ = run_record(tmp_path)
    assert result.returncode == 1
    assert record['summary_comments'] == 0


@pytest.mark.parametrize(('conclusion', 'outcome'), [
    ('failure', 'failure'), ('', 'skipped'), ('', 'failure'),
    ('success', 'failure'), ('failure', 'success'),
])
def test_summary_cannot_hide_unsuccessful_execution(tmp_path, conclusion, outcome):
    result, record, _ = run_record(tmp_path, summaries=[comment()], conclusion=conclusion, outcome=outcome)
    assert result.returncode == 1
    assert record['conclusion'] == 'failure' and record['summary_comments'] == 1


@pytest.mark.parametrize('options', [
    {'api_error': True}, {'malformed': True}, {'started': ''},
    {'summaries': [comment(created_at='invalid')]},
])
def test_missing_or_invalid_evidence_records_failure(tmp_path, options):
    result, record, _ = run_record(tmp_path, **options)
    assert result.returncode == 1
    assert record['conclusion'] == 'failure'
    assert record['summary_comments'] is None
    assert 'private API error details' not in result.stdout + result.stderr


def test_pagination_authorship_and_head_filter(tmp_path):
    old = comment(created_at='2026-09-07T12:00:00Z')
    inline = [old] * 100 + [comment(), comment(commit_id='b' * 40)]
    summaries = [old] * 100 + [comment(body='Reviewed.\n\n' + MARKER)]
    result, record, calls = run_record(tmp_path, inline=inline, summaries=summaries)
    assert result.returncode == 0, result.stderr
    assert (record['inline_comments'], record['summary_comments']) == (1, 1)
    assert len(calls.read_text().splitlines()) == 4
