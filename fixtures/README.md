# Fixtures and the hostile corpus

Contract status: frozen plan. This directory will hold public, replayable fixtures. Fixture bytes are not yet committed; they enter with the deterministic core implementation, byte-frozen, with their manifests and hashes.

## Fixture contract: `proof-check.public-fixture.v0`

Every fixture is a JSON document built from public facts only, replayable offline and deterministically. Each carries a manifest naming its public source (repository, PR number, locators), the observation cutoff, the canonical hash of its content under the same canonicalization rule as receipts (JCS-style canonical JSON, SHA-256 over canonical bytes, no trailing LF), a sanitization statement (no private names, no personal data), and attribution for any quoted material under that material's license. Hashes are verified before evaluation; a fixture whose hash does not match is not evaluated.

## Public cases, frozen

| File | Case | Expected result | Why it exists |
|---|---|---|---|
| `public/cli-cli-pr-14060-review-head.json` | An approval on an older head, three later commits. | Not a V0 `FAIL`. | Regression evidence for why a native-duplicate rule ("approval precedes latest reviewable push") is rejected: GitHub already provides it. |
| `public/github-docs-pr-45651-merge-group.json` | PR-head approval plus merge-group checks, coherent. | `PASS`, detail `MERGE_GROUP_COORDINATES_COHERENT`. | Regression evidence that a generic `review_sha == check_sha` rule is semantically false for merge groups. |
| `public/gentleman-programming-gentle-ai-pr-3890-path-scope.json` | Declared scope "README.md only", 23 changed paths, 22 outside. | `FAIL`, reason `SCOPE_ESCAPE`, detail `PATH_OUTSIDE_DECLARED_SCOPE`, trust `advisory`. | The one V0 product case. |

Sources are public pull requests; attribution and locators travel inside each fixture manifest. Quoted GitHub documentation is attributed under CC-BY.

## Vector families

Each vector states its expected verdict and reason. No network call may change any expected output.

- Scope: the path-scope `FAIL` vector; a sanitized README-only `PASS` twin; a rename whose old or new path escapes, `FAIL` / `SCOPE_ESCAPE`; a glob entry that lawfully covers the diff, `PASS`; an entry refused by the grammar (bare `*`, `..`, universal swallow, duplicate section, empty section), exit `2` or `INDETERMINATE` / `EVIDENCE_AMBIGUOUS`, never `PASS`.
- Stale and drift: fetched head differs from the manifest head, `INDETERMINATE` / `STALE_HEAD` with detail `COORDINATE_DRIFT`; head moves mid-pagination, the same.
- Mixed head: evidence spanning head, test-merge and merge-group SHAs, context-only reporting; the vector asserts V0 does not `FAIL` it.
- Producer and reviewer: a producer-equals-reviewer observation vector asserting `PRODUCER_REVIEWER_CONFLICT` is not emitted by V0 (reserved-code guard).
- Missing and ambiguous: file list truncated at the 3,000-file ceiling or count mismatch, `INDETERMINATE` / `EVIDENCE_MISSING` with detail `CHANGED_PATH_SET_INCOMPLETE`; manifest absent, unbound, or mutable without a hash, `INDETERMINATE`; invalid UTF-8, NUL, or absolute path, `INDETERMINATE` / `EVIDENCE_AMBIGUOUS`; unknown `schema_version`, `INDETERMINATE`.
- Tampered twins: every canonical receipt ships with a byte-tampered twin whose digest must fail verification. A twin that verifies is a release-blocking defect.

## Acceptance bar

Zero incorrect `PASS` across the entire corpus. Any vector whose expected verdict is `FAIL` or `INDETERMINATE` but which returns `PASS` blocks release and falsifies the product choice itself.
