# Security policy

## Reporting

Report a vulnerability privately through GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"). If that route is unavailable, open an issue titled `security contact request` containing no details; a maintainer will answer with a private channel. Please do not post exploit details in public issues or pull requests.

There is no bug bounty. Reports are answered as maintainer time allows.

## Boundary of the tool

This section is part of the frozen public contract and describes what Proof Check touches and what it never touches.

- No code upload, no backend, no account. The tool verifies from public GitHub data and the receipt file alone. It never requires access to any Deedseal service, credential, or private state; if it did, it would fail as proof.
- Data leaving the repository: none. The GitHub Action reads through the job token inside the user's own workflow run. The CLI reads through the user's own token on the user's machine. Receipts contain only public coordinates, public handles, digests, and verdicts.
- Never in a receipt or a log: email addresses, tokens, secret bytes, private source, private repository metadata, model prompts or responses, personal data.
- Read-only permissions. The Action declares exactly `contents: read`, `pull-requests: read`, `checks: read`, `statuses: read`. Every unspecified scope is `none`. No secret beyond the GitHub-provided job token. Any future write used to surface a Check or an artifact is a separate, justified, bounded permission and is never bundled into V0.
- No write to GitHub. The tool creates no workflow, check, comment, label, review, or merge, and executes no code from the inspected pull request.

## What a receipt does not prove

- The runner was honest. GitHub Actions job truth is GitHub-owned; a receipt cannot prove otherwise.
- The provider behind a green status actually tested the bytes.
- Current GitHub state, when verified offline.
- Anything about the correctness, quality, or authorization of the change.

Every receipt restates these limits in its `does_not_prove` field. Author-controlled scope declarations are advisory evidence, never security policy.

## Supported versions

Nothing is released yet. This repository currently holds the public contract only.
