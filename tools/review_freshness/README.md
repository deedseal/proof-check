# Review Freshness

`check.py` is a read-only merge check. It is `FRESH` only when GitHub records a
completed, successful run of `.github/workflows/claude-review.yml` for the
pull request's current head SHA. It reads pull-request metadata and Actions
workflow runs; it never reads comments.

Closed stale reasons are `NO_RUN_FOR_HEAD`, `RUN_NOT_COMPLETED`,
`RUN_NOT_SUCCESS`, and `API_ERROR`. API failures always fail closed.

```console
GITHUB_TOKEN=... python3 tools/review_freshness/check.py \
  --repository owner/repository --pr-number 42 --head-sha "$HEAD_SHA"
```

Exit status is 0 for `FRESH`, 10 for `STALE`, and 2 for invalid usage or
configuration. Standard output contains exactly one JSON result object.

For an opened or newly-ready pull request, the repository workflow polls this
one-shot decision while the reviewer is running. A later push is checked once
and becomes stale immediately because the reviewer does not run on pushes.
