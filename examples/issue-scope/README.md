# Issue-bound scope example

An executable UX experiment, not a new Proof Check contract or release. It reuses the released
`deedseal/proof-check@363aad91142a01df6e5d72a87495d2f09be28823` Action unchanged; no provider API key, GitHub App, backend, or new account.

## Setup

1. Open a repository Issue whose entire body is exactly one `## Allowlist` heading followed by one path, glob, or `dir/**` prefix per line — no bullet marker, no other heading or prose; [`proof-check.yml`](proof-check.yml) refuses anything else in the body.
2. In the PR body, add exactly one line: `Scope-Issue: #<number>`.
3. Copy [`proof-check.yml`](proof-check.yml) into `.github/workflows/`.

## What it does

Reads the PR body's `Scope-Issue:` line from the event payload (never interpolated into a shell command), reads that Issue via `GITHUB_TOKEN` (`issues: read`), writes its body bytes to `bundle/declaration.txt`, and generates `bundle/policy.json` with `scope.source: pinned:<sha256 of those bytes>` and `scope.trust: advisory` before running the released Action. Proof Check returns `PASS`, `FAIL`, or `INDETERMINATE`; `bundle/` uploads as an offline-verifiable artifact either way.

It fails closed, before the Action runs, on a missing/duplicated/malformed `Scope-Issue` line, a reference to another repository or a pull request, an unavailable or closed Issue, or a body that is missing, not UTF-8, lacks a single `## Allowlist` section, or whose section is empty, duplicated, or shares the body with other content. A changed path outside the declaration, or a receipt head unequal to the PR event head, are refused by the released Action itself, unchanged.

## Honest boundary

- The digest binds the receipt to the Issue-body bytes this run observed.
- It does not prove who originally wrote the Issue, or stop an authorized person from editing it later.
- A later Issue edit needs a fresh workflow run to produce new evidence.
- Proof Check checks declared scope, not code correctness or merge approval.
