# Proof Check

**Status: not released.** There is no Action to install and no CLI to download yet. This repository currently holds the public contract that the tool will obey: verdict semantics, the receipt and policy schemas, the fixture plan, and the claims boundary. Code follows the contract, not the other way round.

## What it is

Proof Check is a proposed free GitHub Action and CLI that checks whether one pull request's declared evidence still coheres around its current head, explains any gap, and produces a receipt another person can verify.

Category: PR evidence-coherence check with a portable verification receipt.

The question it answers for one pull request at one exact commit: do the changed paths stay inside the scope the change declared, and is the evidence complete enough to say so? The answer is one of three words:

| Verdict | Meaning |
|---|---|
| `PASS` | The declared rules hold for this exact commit at the named cutoff. Nothing more. |
| `FAIL` | The change touched paths outside its declared scope. The receipt names them. |
| `INDETERMINATE` | The tool does not have enough evidence to say either honestly. The job fails rather than guessing green. |

Every verdict comes with a plain-language explanation and a JSON receipt: what was checked, at which commit, with what result, verifiable offline without trusting Proof Check or the machine that produced it.

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

Not yet. When a release exists, it will be pinned by full commit SHA in the examples, with a major tag offered separately as a convenience. Until then nothing here is runnable.

## Planned permissions

The Action will declare exactly `contents: read`, `pull-requests: read`, `checks: read`, `statuses: read`, and nothing else. It will create no workflow, check, comment, label, review, or merge.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
