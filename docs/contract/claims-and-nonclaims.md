# Public claims and non-claims

Contract status: frozen. Binding on every public word in this repository: README, listing text, release notes, examples, comments in code.

## What may be claimed

Positioning sentence, verbatim:

> Proof Check is a proposed free GitHub Action and CLI that checks whether one pull request's declared evidence still coheres around its current head, explains any gap, and produces a receipt another person can verify.

Category, verbatim:

> PR evidence-coherence check with a portable verification receipt.

`PASS` is evidence coherence under the declared rules at a named cutoff. Nothing more.

## What is never claimed

- Proof Check does not decide whether code is correct, safe, valuable, compliant, approved, or ready to merge.
- It does not make GitHub checks, reviews, branch protections, releases, or attestations unnecessary.
- A receipt is not certification.
- A receipt does not remove trust in GitHub, repository permissions, the evaluated inputs, the runner, or the reader's chosen verifier.
- The tool cannot prove that a provider actually tested the bytes behind a green status, and must say so.
- Offline verification never claims currentness of GitHub state.

## Claim labels

Every published factual statement carries exactly one label and cannot move category by prose:

| Label | Meaning |
|---|---|
| `OBSERVED FACT` | Read from a public source that is named and locatable. |
| `DEEDSEAL-ACCEPTED PRIVATE FACT` | Accepted inside Deedseal's private record; cited by locator only, never reproduced. |
| `PRODUCT CLAIM` | A statement about what the product intends to do. Not evidence. |

Market statements inherited from research (for example, that no current listing combines a given set of properties) are absence of evidence in a reviewed set, not proof of absence. They stay labeled `UNKNOWN` as market claims and are never used as public superiority claims.

## Prohibited or gated vocabulary

The following words and phrases do not appear in public text unless directly earned by an accepted public record, which today none of them is. This list is itself the only place they may appear in this repository.

`revolutionary`, `unhackable`, `fully autonomous`, `exactly once`, `secure`, `safe`, `tamper-proof`, `certified`, `complete proof`, `provider-neutral`, `production-ready`, `Marketplace verified`, `GitHub-approved`, `partner`, `unique`, `first`.

Also prohibited without their own accepted public record: any superiority, savings, SLA, adoption, partnership, or customer-outcome claim; any price, date, or availability statement.

A verified-creator badge is neither assumed nor required and must never be implied. Provider names are evidence coordinates only, never ranked or headlined.

## Enforcement

A word-boundary grep for the prohibited list over the whole tree, excluding this file, is part of the acceptance of every change. Publication of anything beyond this repository remains a separate, Owner-gated act.
