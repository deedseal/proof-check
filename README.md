# Proof Check

**Status: released as [v0.1.1](https://github.com/deedseal/proof-check/releases/tag/v0.1.1).** The Action wrapper is pinned at `363aad91142a01df6e5d72a87495d2f09be28823`. `v0.1.0` is superseded and must not be used from another repository. Later changes on `main` add repository review workflows and their supporting tools and tests; the released Action code is unchanged. The public contract remains the authority for verdict semantics, receipt and policy schemas, fixture plan, and claims boundary. Code follows the contract, not the other way round.

## What it is

Proof Check is a free GitHub Action and CLI that checks whether one pull request's declared evidence still coheres around its current head, explains any gap, and produces a receipt another person can verify.

Category: PR evidence-coherence check with a portable verification receipt.

The question it answers for one pull request at one exact commit: do the changed paths stay inside the scope the change declared, and is the evidence complete enough to say so? The answer is one of three words:

| Verdict | Meaning |
|---|---|
| `PASS` | The declared rules hold for this exact commit at the named cutoff. Nothing more. |
| `FAIL` | The change touched paths outside its declared scope. The receipt names them. |
| `INDETERMINATE` | The tool does not have enough evidence to say either honestly. The job fails rather than guessing green. |

Every verdict comes with a plain-language explanation and a JSON receipt: what was checked, at which commit, with what result. The receipt's integrity can be checked offline without trusting Proof Check; what it observed still rests on GitHub and on the runner that produced it, as [`SECURITY.md`](SECURITY.md) spells out.

## What it is not

- It does not decide whether code is correct, valuable, compliant, approved, or ready to merge.
- It does not replace GitHub checks, reviews, branch protections, releases, or attestations. Stale-approval dismissal, required checks, up-to-date branches and merge queues are GitHub's; Proof Check does not re-implement them.
- A receipt is not certification, and it does not remove trust in GitHub, repository permissions, the evaluated inputs, the runner, or the reader's chosen verifier.
- No code upload, no backend, no account. It reads GitHub data with read-only permissions inside your own workflow or on your own machine.

The full boundary is in [`docs/contract/claims-and-nonclaims.md`](docs/contract/claims-and-nonclaims.md) and [`SECURITY.md`](SECURITY.md).

## The contract

| Document | Freezes |
|---|---|
| [`docs/contract/verdicts-and-reason-codes.md`](docs/contract/verdicts-and-reason-codes.md) | The three verdicts, the closed reason-code vocabulary, detail codes, exit codes `0 / 10 / 20 / 2`, CLI surface. |
| [`docs/contract/subject-semantics.md`](docs/contract/subject-semantics.md) | `head` vs `test_merge` vs `merge_group`, and the required-check `skipped` / `neutral` trap. |
| [`docs/contract/claims-and-nonclaims.md`](docs/contract/claims-and-nonclaims.md) | What may be said publicly, claim labels, prohibited vocabulary. |
| [`schemas/proof-check-receipt.v1.schema.json`](schemas/proof-check-receipt.v1.schema.json) | `proof-check-receipt/v1`, including canonicalization and digest rules. |
| [`schemas/proof-check-policy.v1.schema.json`](schemas/proof-check-policy.v1.schema.json) | `proof-check-policy/v1`, including the declared-scope entry grammar. |
| [`fixtures/README.md`](fixtures/README.md) | The public fixture contract, the hostile corpus plan, and the acceptance bar: zero incorrect `PASS`. |

## GitHub Action

The Marketplace's "Use latest version" snippet supplies `@v0.1.1`. The Action refuses tags and branches with exit `2` and the message `action reference must be a full 40-hex commit; tags and branches are not pins`. Use `uses: deedseal/proof-check@363aad91142a01df6e5d72a87495d2f09be28823` instead.

### Consumer workflow

Save this as `.github/workflows/proof-check.yml` in your repository. It follows this repository's [bundle workflow](.github/workflows/proof-check.yml), with the remote Action and the separate verifier checkout pinned to the release. It reads the PR checkout as data and installs the verifier from the pinned `trusted` checkout.

```yaml
name: Proof Check

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]

permissions:
  contents: read
  pull-requests: read
  checks: read
  "statuses": read

jobs:
  proof-check:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Check out the PR head as data
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          path: subject
          persist-credentials: false
      - name: Check out the released offline verifier
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with:
          repository: deedseal/proof-check
          ref: 363aad91142a01df6e5d72a87495d2f09be28823 # v0.1.1
          path: trusted
          persist-credentials: false
      - name: Prepare offline bundle
        shell: bash
        run: |
          mkdir proof-check-bundle
          cp subject/examples/action/policy.json proof-check-bundle/policy.json
          python3 - <<'PY_BODY'
          import json, os
          from pathlib import Path
          event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
          Path('proof-check-bundle/declaration.txt').write_bytes(
              (event['pull_request']['body'] or '').encode('utf-8'))
          PY_BODY
      - name: Evaluate the PR head
        uses: deedseal/proof-check@363aad91142a01df6e5d72a87495d2f09be28823 # v0.1.1
        with:
          policy: proof-check-bundle/policy.json
          declaration: proof-check-bundle/declaration.txt
          receipt: proof-check-bundle/receipt.json
          target: head
      - name: Verify receipt offline
        if: ${{ always() && hashFiles('proof-check-bundle/receipt.json') != '' }}
        shell: bash
        run: |
          python3 -m venv "$RUNNER_TEMP/proof-check-verify"
          "$RUNNER_TEMP/proof-check-verify/bin/python" -m pip install ./trusted
          "$RUNNER_TEMP/proof-check-verify/bin/proof-check" verify \
            proof-check-bundle/receipt.json --offline-bundle proof-check-bundle
      - name: Upload offline bundle
        if: always()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: proof-check-offline-bundle-${{ github.run_id }}-${{ github.run_attempt }}
          path: proof-check-bundle/
          if-no-files-found: error
```

The workflow uses read-only `contents`, `pull-requests`, `checks`, and `statuses` permissions. The Action uploads its receipt when one exists, including a non-PASS receipt; the workflow also uploads the policy and PR declaration for offline verification. Invalid configuration can fail before a receipt exists. A `FAIL` or `INDETERMINATE` receipt makes the Action step red, including when the CLI policy opts out of a nonzero exit for `INDETERMINATE`. The Action refuses a receipt whose head differs from the PR event head.

This is a `pull_request` workflow: the PR can change its workflow and policy. A PR-body declaration is advisory. Making checks required for merge and restricting who can change their workflows require repository-protection settings; a red optional check alone does not prohibit merging. Proof Check does not create those settings or enforce review freshness.

### Policy file

Save this as `examples/action/policy.json` in your repository; the workflow copies it into the offline bundle.

```json
{
  "schema_version": "proof-check-policy/v1",
  "scope": {
    "source": "pr_body_allowlist",
    "trust": "advisory"
  },
  "checks": {
    "required_selectors": []
  },
  "reviews": {
    "policy": "report_only"
  },
  "verdict_policy": {
    "fail_exit": 10,
    "indeterminate_exit": 20
  },
  "receipt": {
    "mode": "always"
  }
}
```

| Field | Meaning |
|---|---|
| `schema_version` | Selects the `proof-check-policy/v1` schema. |
| `scope.source` | Reads the PR body's `## Allowlist` section. |
| `scope.trust` | Labels that author-controlled declaration as `advisory`. |
| `checks.required_selectors` | The empty list configures no required check selectors. |
| `reviews.policy` | `report_only` records review context without applying a review rule. |
| `verdict_policy.fail_exit` | Sets the CLI's `FAIL` exit code to `10`. |
| `verdict_policy.indeterminate_exit` | Sets the CLI's `INDETERMINATE` exit code to `20`. |
| `receipt.mode` | `always` requests a receipt for every evaluated verdict. |

### PR declaration

The PR body must contain exactly one `## Allowlist` section, with one repository-relative path or glob per bullet. For a change that adds the workflow and policy above and edits the README, copy this into the PR body and adjust the paths to the intended scope:

```markdown
## Allowlist
- .github/workflows/proof-check.yml
- examples/action/policy.json
- README.md
```

From the [policy schema](schemas/proof-check-policy.v1.schema.json), verbatim:

> Declared-scope entry grammar, adopted verbatim from prior art proven in production CI: one entry per line under a single '## Allowlist' markdown section when the source is a PR body (exactly one such section; zero or duplicates fail CLOSED); each entry is an exact repository-relative path, an fnmatch glob, or a directory prefix ending in '/**'. Fail-closed grammar refusals: bare '*' or '**'; a leading '/'; '..' path traversal; an empty prefix before '/**'; any entry broad enough to swallow the universal improbable probe path (checked at runtime). No section, an empty section, or an out-of-list path all fail CLOSED. Author-controlled declarations remain advisory even when frozen and hashed.

From the [fixture contract](fixtures/README.md), verbatim:

> Scope: the path-scope `FAIL` vector; a sanitized README-only `PASS` twin; a rename whose old or new path escapes, `FAIL` / `SCOPE_ESCAPE`; a glob entry that lawfully covers the diff, `PASS`; an entry refused by the grammar (bare `*`, `..`, universal swallow, duplicate section, empty section), exit `2` or `INDETERMINATE` / `EVIDENCE_AMBIGUOUS`, never `PASS`.

With the PR-body policy above, a missing, duplicate, empty, or malformed allowlist yields `INDETERMINATE` and a red Action step. A changed path outside a valid list yields `FAIL` with `SCOPE_ESCAPE`. Invalid policy configuration can instead stop with exit `2`. Keep the declaration narrow enough to name the intended change; do not add paths merely to hide a scope escape.

### Verify the downloaded bundle

Download and extract the `proof-check-offline-bundle-<run_id>-<run_attempt>` artifact into `proof-check-bundle`. With the CLI installed from the release commit (see below), run:

```bash
proof-check verify proof-check-bundle/receipt.json --offline-bundle proof-check-bundle
```

This checks the recorded receipt and bundled inputs without fetching current GitHub state; it does not establish that the PR is still at the recorded head.

## Install the CLI from source

Install from a full 40-character source commit. The commands below use the `v0.1.1` release commit; a branch name is not a pin.

```bash
git clone https://github.com/deedseal/proof-check.git
cd proof-check
git checkout --detach 363aad91142a01df6e5d72a87495d2f09be28823
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
proof-check --help
```

The CLI records the source commit in every receipt. `check` refuses with exit `2` when the installed bytes cannot be associated with a recorded source commit.

## CLI usage

Identify one pull request with `--repo <owner/name>` and `--pr <number>`. The default target is the pull request head; `--target test_merge` selects GitHub's synthetic merge commit. `merge_group` requires event context and is refused by this local command.

```bash
export GITHUB_TOKEN=<your-read-only-token>
proof-check check \
  --repo example-org/example-repo \
  --pr 42 \
  --policy policy.json \
  --receipt proof-check-receipt.json

proof-check verify proof-check-receipt.json \
  --offline-bundle examples/offline-bundle
```

For `scope.source: pinned:<sha256>`, pass the declaration bytes with `--declaration <file>`. Offline verification reads `policy.json` and, when present, `declaration.txt` from the bundle directory. Without `--offline-bundle`, `verify` uses the token to observe the pull request again and labels head, file-count, and check differences; it does not change the recorded verdict.

Exit codes are `0` for `PASS` or a verified receipt, `10` for `FAIL`, `20` for `INDETERMINATE` or an unverifiable receipt, and `2` for invalid input, configuration, or usage.

## Permissions and data

The CLI makes only `GET` requests beneath `https://api.github.com/repos/<owner>/<name>/`. Supply a token through the environment variable named by `--token-env` (default `GITHUB_TOKEN`) with read access to repository contents, pull requests, and checks. Token bytes are not placed in receipts, stdout, or stderr. The reader does not download or execute code from the inspected pull request; a `file:` policy reads only its named manifest from the base commit.

There is no Deedseal account, backend, journal, or upload. GitHub receives the API requests needed to read the named pull request; the receipt is written on the user's machine. It contains repository coordinates, public handles, digests, and the verdict, but not email addresses, token bytes, source contents, or model prompts and responses. When the user checks a private repository, the locally written receipt discloses `subject.repository` (the private repository name), `head_ref`/`base_ref`, changed paths, check names, and reviewer handles. This deviates from `SECURITY.md`'s current private-metadata statement until the contract is amended; handle the receipt under the repository's own disclosure rules.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
