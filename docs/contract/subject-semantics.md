# Subject semantics: head, test-merge and merge group

Contract status: frozen. Every receipt names exactly one `subject.target_kind`.

## The three subject kinds

| `target_kind` | What it is | Where GitHub exposes it |
|---|---|---|
| `head` | The PR head commit, the branch's own tip. This is what a reviewer's `commit_id` and most external check `head_sha` values bind to. | `github.event.pull_request.head.sha` |
| `test_merge` | The synthetic merge commit GitHub maintains for the PR. In a `pull_request`-triggered workflow `GITHUB_SHA` is the last merge commit of the pull request merge branch and `GITHUB_REF` is `refs/pull/<N>/merge`. A plain `pull_request` job therefore does not run on the head SHA by default. | `refs/pull/<N>/merge` |
| `merge_group` | The commit a merge queue builds when a PR is added to a merge group. The `merge_group` event runs the workflow against the merge-group SHA and ref. That SHA can legitimately differ from both head and test-merge and can become the squash or merge commit. | `merge_group` event payload |

Source for the three definitions: GitHub documentation, "Events that trigger workflows" (`pull_request`, `merge_group`), read at the contract freeze.

## Consequences frozen into the contract

1. Evidence bound to different SHAs across these three kinds is not incoherence by itself. Equality checks across kinds are forbidden. V0 records the kinds and SHAs verbatim and reports mixture as context (`MIXED_HEAD_EVIDENCE`, reserved, context-only).
2. The declared-scope rule evaluates the PR's changed-path set at the bound head/base tuple only. It never reconstructs merge-queue state.
3. The tool reads the final head again after collecting evidence. If it moved, the verdict is `INDETERMINATE` / `STALE_HEAD` with detail `COORDINATE_DRIFT`.

## The required-check trap

GitHub documents that successful check statuses for required status checks are `success`, `skipped` and `neutral`. A conditionally skipped job reports Success and does not block merging. (A workflow skipped by path or branch filters instead stays Pending and blocks; Proof Check relies on neither behavior for its honesty.)

Therefore:

- the Proof Check job itself must exit non-zero on `FAIL` and on `INDETERMINATE`;
- it must never map either verdict to a `skipped` or `neutral` conclusion;
- a policy that lets `INDETERMINATE` pass must be an explicit, visible opt-out, never a default.

## What this document does not claim

Staleness detection between a review and a later push duplicates native GitHub controls (dismiss stale approvals, require approval of the most recent reviewable push, strict up-to-date branches, merge queues). Proof Check does not re-implement them and claims no novelty over them. The one non-duplicative signal in V0 is the deterministic comparison of a declared, frozen path-scope manifest against the PR's complete changed-path set at an exact head, plus honest `INDETERMINATE` reporting and a portable, offline-verifiable receipt.
