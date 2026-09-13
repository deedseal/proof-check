# N28 — Builder branch confinement: design and offline proof

Status: **design and offline proof only.** Nothing in this packet was applied. The live gate is
`OWNER_RULESET_ACT_REQUIRED`. No ruleset, branch protection, credential, GitHub App installation,
secret, tag, or actuator was created, updated or deleted to produce this document. No live push was
attempted. Configuration bytes are not confinement evidence.

Packet: Issue #36. Parent block: Issue #23, packet N28. This document does not authorize unattended
repair and does not choose the dispatcher.

## 1. The question

Before any unattended repair loop is enabled, one fact must hold: the **actual** Builder GitHub App
identity can update exactly one assigned branch, and is refused on `main`, on another worker's
branch, on tags, and on every other ref.

## 2. Why `builder/**` alone is insufficient

Issue #23 sketched N28 as "a `builder/**` ruleset restricting updates to the Builder identity".
That sketch is rejected here.

A ruleset is a **deny** rule keyed on a ref pattern, plus a bypass list. Restricting
`creation`/`update`/`deletion` on `builder/**` with the Builder App as the single bypass actor
produces this: *every principal except the Builder App is kept out of the whole `builder/**`
namespace, and the Builder App is admitted to all of it.* That is the opposite of the requirement.
It confines other principals out of a directory; it does not confine one credential to one branch.
`builder/**` matches an unbounded set of refs, including refs assigned to other workers and refs
that do not exist yet. Directory-wide targeting therefore cannot be the assignment rule, and the
offline tests in `tests/test_builder_confinement.py` reject it.

## 3. What GitHub can and cannot express

Verified against GitHub's current primary documentation (REST "Create a repository ruleset", and
"Available rules for rulesets") on 2026-09-09:

| Rule type | Documented meaning |
|---|---|
| `creation` | "Only allow users with bypass permission to create matching refs." |
| `update` | "Only allow users with bypass permission to update matching refs." |
| `deletion` | "Only allow users with bypass permissions to delete matching refs." |

`bypass_actors[].actor_type` accepts `Integration`, `OrganizationAdmin`, `RepositoryRole`, `Team`,
`DeployKey`, `User`. A GitHub App is an `Integration` and is identified by an exact integer
`actor_id`. `conditions.ref_name.include` accepts exact ref names, `fnmatch` patterns, `~ALL` and
`~DEFAULT_BRANCH`. `target` is `branch`, `tag` or `push`; tags are a separate target.

**What GitHub does not express.** There is no primitive of the form "App X may write only
`refs/heads/Y`". An App installation carries repository-wide `contents: write`; ref-level narrowing
exists only as ruleset denials. Confinement is therefore always a *composition*: deny over the ref
space, with exactly one exemption. This packet delivers the exemption half exactly, and names the
denial half as an explicit Owner precondition (§6). That asymmetry is stated rather than hidden.

## 4. The proposed rule

One ruleset, bound to one exact disposable assigned ref, one exact Integration actor, and no other
bypass actor.

- `target` is `branch`. A tag ruleset is a different object and is out of this rule.
- `enforcement` is `active`. `evaluate` and `disabled` do not deny anything.
- `conditions.ref_name.include` is exactly one exact ref, `refs/heads/builder/n28-assignment-0001`.
  It contains no `fnmatch` metacharacter, is not `~ALL`, is not `~DEFAULT_BRANCH`, and is not a tag
  ref. The branch is disposable and does not exist; this packet does not create it.
- `conditions.ref_name.exclude` is empty. A non-empty exclude list can only widen what escapes the
  rule.
- `rules` is exactly `creation`, `update`, `deletion`. Dropping any one of the three leaves a
  matching operation unrestricted.
- `bypass_actors` is exactly one entry: `actor_type: "Integration"`, `bypass_mode: "always"`, and
  the exact Builder App `actor_id`. A second entry, a `User`, a `Team`, an `OrganizationAdmin`, a
  `RepositoryRole` or a `DeployKey` entry would each admit a principal other than the Builder App.

Payload — `examples/rulesets/n28-builder-confinement.json`, byte-for-byte:

```json
{
  "name": "n28-builder-assignment-0001",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [
    {
      "actor_id": "<BUILDER_APP_ID>",
      "actor_type": "Integration",
      "bypass_mode": "always"
    }
  ],
  "conditions": {
    "ref_name": {
      "include": [
        "refs/heads/builder/n28-assignment-0001"
      ],
      "exclude": []
    }
  },
  "rules": [
    {
      "type": "creation"
    },
    {
      "type": "update"
    },
    {
      "type": "deletion"
    }
  ]
}
```

### The one substitution slot

`bypass_actors[0].actor_id` ships as the string `"<BUILDER_APP_ID>"`. The Builder App has not been
chosen (Issue #23 records that kbp-dev-office#1267 is still open), so no honest integer exists in
this repository yet. The placeholder is a **string**, so the file as shipped is rejected by the API
rather than silently applied with a wrong actor; that is the intended fail-closed behaviour. It is
the only slot: the ref, the rule set, the actor type, the bypass mode and the enforcement are exact.

The Owner resolves it from the installation record, not from prose:

```bash
gh api /repos/deedseal/proof-check/installations \
  --jq '.installations[] | {app_id: .app_id, app_slug: .app_slug, id: .id}'
```

`app_id` is the `Integration` `actor_id`. `id` is the installation id and is **not** the actor id.

## 5. Applying it (Owner act, not this packet)

```bash
# 1. Substitute the resolved integer, without editing the repository file.
BUILDER_APP_ID=<the app_id read above>
jq --argjson app_id "$BUILDER_APP_ID" \
   '.bypass_actors[0].actor_id = $app_id' \
   examples/rulesets/n28-builder-confinement.json > /tmp/n28-ruleset.json

# 2. Inspect, then apply once.
gh api --method POST /repos/deedseal/proof-check/rulesets --input /tmp/n28-ruleset.json
```

## 6. Preconditions the Owner must hold, and the residual

This ruleset makes the assigned ref **exclusive to the Builder App**. On its own it does not refuse
the Builder anywhere else. The other refusals come from rulesets the Owner holds separately:

| Ref | Refusal comes from | State today |
|---|---|---|
| `main` | `main-protection` (id `22597587`): `~DEFAULT_BRANCH`, rules `deletion`, `non_fast_forward`, `pull_request`, `required_status_checks`, and `bypass_actors: []` — an empty bypass list denies every principal, the Builder App included. | Exists. **Must be preserved unchanged**; this packet does not modify it. |
| Another worker's assigned ref | That worker's own exact-ref ruleset, whose bypass list names its own actor and not this Builder App. | Per-assignment Owner act. |
| Any other `builder/**` ref | A namespace deny ruleset over `refs/heads/builder/**` that *excludes* the assigned ref and carries `bypass_actors: []`. | Not applied. Owner act. |
| Tags | A `target: "tag"` ruleset over `~ALL` with `bypass_actors: []`. | Not applied. Owner act. |

Read-back before and after any Owner act, so that "unchanged" is a checked fact:

```bash
gh api /repos/deedseal/proof-check/rulesets/22597587 \
  --jq '{name, target, enforcement, conditions, bypass_actors, rules: [.rules[].type]}'
```

Expected, unchanged: `name` `main-protection`, `target` `branch`, `enforcement` `active`,
`conditions.ref_name.include` `["~DEFAULT_BRANCH"]`, `bypass_actors` `[]`, rule types
`["deletion", "non_fast_forward", "pull_request", "required_status_checks"]`.

**Residual, stated plainly.** Until the three Owner acts in the table above are applied, the Builder
App's credential is confined only where a ruleset already denies it. This packet closes the
assignment half exactly and closes nothing else.

## 7. Offline proof

`tests/test_builder_confinement.py` runs with no network. It pins the shipped payload's exact shape,
asserts that this document's fenced payload block matches the file byte-for-byte, and rejects a
hostile mutation corpus. The reason-code vocabulary is closed:

| Reason code | Rejects |
|---|---|
| `PAYLOAD_NOT_OBJECT` | A payload that is not a JSON object. |
| `EXTRA_TOP_LEVEL_KEY` | Any key beyond the six named in §4. |
| `NAME_MISSING` | Absent, empty or non-string `name`. |
| `NAME_RESERVED` | A name that would collide with an existing Owner ruleset (`main-protection`, `smoke containment`). |
| `TARGET_NOT_BRANCH` | `target` of `tag` or `push`. |
| `ENFORCEMENT_NOT_ACTIVE` | `evaluate` or `disabled`. |
| `CONDITIONS_MALFORMED` | Missing or non-object `conditions` / `ref_name`, or non-list include/exclude. |
| `INCLUDE_NOT_EXACTLY_ONE` | Zero, two or more included refs. |
| `INCLUDE_DEFAULT_BRANCH` | `~DEFAULT_BRANCH`, `main`, `refs/heads/main`. |
| `INCLUDE_TAG_REF` | `refs/tags/...`. |
| `INCLUDE_NOT_EXACT_REF` | `~ALL`, `*`, `builder/**`, `refs/heads/builder/*`, a missing `refs/heads/` prefix, and any ref `git check-ref-format` would refuse: `..`, `//`, a trailing `/`, a `.lock` suffix, `@{`, a space, a control character, or one of `~ ^ : ? * [` and backslash. |
| `EXCLUDE_NOT_EMPTY` | Any excluded ref. |
| `RULES_NOT_EXACT_SET` | A missing or additional rule type, a duplicate, or a malformed rule entry. |
| `BYPASS_NOT_EXACTLY_ONE` | Zero or more than one bypass actor. |
| `BYPASS_ACTOR_TYPE_NOT_INTEGRATION` | `User`, `Team`, `OrganizationAdmin`, `RepositoryRole`, `DeployKey`. |
| `BYPASS_MODE_NOT_ALWAYS` | `pull_request` or `exempt`. |
| `BYPASS_ACTOR_ID_INVALID` | A non-integer, boolean, zero, negative or absent actor id — including the shipped `"<BUILDER_APP_ID>"` placeholder when the payload is checked as resolved. |

## 8. What is still not proven, and how to prove it

Offline tests prove that the payload says what §4 says. They prove nothing about GitHub's runtime.
The live proof is an Owner act on **disposable** refs and is not authorized by this packet:

1. Apply the resolved ruleset. Read it back and record the returned `id` and `bypass_actors`.
2. Have the actual Builder credential push to `refs/heads/builder/n28-assignment-0001`. Expect
   success.
3. Have the same credential attempt `main`, another worker's assigned ref, and a tag. Expect refusal
   on each, recorded as the API status and message.
4. Have a non-Builder principal attempt the assigned ref. Expect refusal.
5. Read back `main-protection` (§6) and confirm it is byte-identical to the pre-act read.
6. Delete the disposable refs and the ruleset.

Steps 2–4 are the only evidence that the credential is confined. Until they are recorded against an
exact head, no report may describe Builder confinement as proven, and unattended repair stays off.
