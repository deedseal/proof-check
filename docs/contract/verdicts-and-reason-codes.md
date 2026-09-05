# Verdicts, reason codes and exit codes

Contract status: frozen for rules version `rules/v0`. This document is normative for the receipt schema (`schemas/proof-check-receipt.v1.schema.json`). A change to any table below requires a new frozen contract version; prose cannot widen it.

## Verdicts

Every evaluation ends in exactly one of three verdicts, always bound to an exact evaluated SHA and an observation cutoff:

| Verdict | Meaning | Effect on the job |
|---|---|---|
| `PASS` | The declared rules hold for the evaluated SHA at the named cutoff. Nothing more. | exit `0` |
| `FAIL` | A declared rule is violated at the evaluated SHA. | exit `10` |
| `INDETERMINATE` | Required input is absent, incomplete or ambiguous, or the subject moved during evaluation. The tool does not know, and says so. | exit `20` |

`UNKNOWN` required inputs force `INDETERMINATE`, never a best-effort `PASS`. One incorrect `PASS` across the hostile corpus falsifies the product choice, not just the release.

`INDETERMINATE` is not a weak third option. It is the one word a green tick cannot say.

## Top-level reason codes (closed vocabulary)

| Reason code | `rules/v0` status | Verdict it may accompany | Meaning |
|---|---|---|---|
| `SCOPE_ESCAPE` | active | `FAIL` | The only `FAIL` rule in V0: the PR's complete changed-path set contains a path outside the declared scope. Detail code `PATH_OUTSIDE_DECLARED_SCOPE`; the sorted outside-path list is in `outside_paths`. |
| `EVIDENCE_MISSING` | active | `INDETERMINATE` | A required input is absent: manifest missing or unbound, changed-path set incomplete (detail `CHANGED_PATH_SET_INCOMPLETE`), API error, fork head unavailable. |
| `EVIDENCE_AMBIGUOUS` | active | `INDETERMINATE` | Input present but not decidable: invalid path encoding, normalization collision, mutable manifest without a frozen hash, unknown schema version. |
| `STALE_HEAD` | active as honesty context only | `INDETERMINATE` | The observed head moved off the bound coordinates during evaluation (detail `COORDINATE_DRIFT`). Reported context, not claimed novelty: staleness detection duplicates native GitHub controls and is never a V0 `FAIL` rule. |
| `MIXED_HEAD_EVIDENCE` | reserved (context-only reporting permitted) | `INDETERMINATE` | Evidence objects resolve to different SHAs (head, test-merge, merge-group). V0 may report the observation; it must not judge it. A naive equality rule is semantically false for merge refs and merge groups. |
| `PRODUCER_REVIEWER_CONFLICT` | reserved | none in V0 | No V0 rule. Kept in the closed vocabulary for a later, separately frozen rule version. |

Rules:

- The vocabulary is closed. No new top-level code without a new frozen contract version.
- A reserved code appearing as a blocking reason in V0 output is itself a defect.
- `PASS` carries zero reason codes.

## Rule-level detail codes

Detail codes live in the separate `detail_codes` field and never widen the top-level set. Closed per `rules_version`:

| Detail code | Emitted with |
|---|---|
| `PATH_OUTSIDE_DECLARED_SCOPE` | `FAIL` / `SCOPE_ESCAPE` |
| `ALL_CHANGED_PATHS_WITHIN_DECLARED_SCOPE` | `PASS` |
| `CHANGED_PATH_SET_INCOMPLETE` | `INDETERMINATE` / `EVIDENCE_MISSING` |
| `COORDINATE_DRIFT` | `INDETERMINATE` / `STALE_HEAD` |
| `MERGE_GROUP_COORDINATES_COHERENT` | `PASS` (context: merge-group coordinates observed and coherent) |

## Exit codes (closed)

| Exit | Meaning |
|---|---|
| `0` | `PASS` (for `check`); receipt verified (for `verify`) |
| `10` | `FAIL` |
| `20` | `INDETERMINATE`; for `verify`, an unverifiable receipt |
| `2` | invalid input, configuration or usage; for `verify`, malformed input |

No other exit expresses a verdict. A policy that maps `INDETERMINATE` to exit `0` must be an explicit, visible opt-out recorded in the receipt, never a default.

## CLI surface bound to these codes

```text
proof-check check  --policy <file> --receipt <out.json>
proof-check verify <receipt.json> [--offline-bundle <dir>]
```

`verify` without `--offline-bundle` re-observes current GitHub state and labels the difference; with it, it proves canonical consistency, schema closure and digest/provenance only, and says so.
