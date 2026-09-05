# Proof Check

**Status: not released.** There is no Action or packaged release to install. The CLI can be installed locally from a source commit; the public contract remains the authority for its verdict semantics, receipt and policy schemas, fixture plan, and claims boundary. Code follows the contract, not the other way round.

## What it is

Proof Check is a proposed free GitHub Action and CLI that checks whether one pull request's declared evidence still coheres around its current head, explains any gap, and produces a receipt another person can verify.

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
- No code upload, no backend, no account. It reads public GitHub data with read-only permissions inside your own workflow or on your own machine.

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

## Install

Choose the full 40-character commit you reviewed, then install that checkout. A branch name is not a pin.

```bash
git clone https://github.com/deedseal/proof-check.git
cd proof-check
git checkout --detach <40-character-commit>
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

There is no Deedseal account, backend, journal, or upload. GitHub receives the API requests needed to read the named pull request; the receipt is written on the user's machine. It contains repository coordinates, public handles, digests, and the verdict, but not email addresses, token bytes, source contents, or model prompts and responses. When the user checks a private repository, `subject.repository` contains that private repository name in the locally written receipt; handle that file under the repository's own disclosure rules.

## Planned permissions

The Action will declare exactly `contents: read`, `pull-requests: read`, `checks: read`, `statuses: read`, and nothing else. It will create no workflow, check, comment, label, review, or merge.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
