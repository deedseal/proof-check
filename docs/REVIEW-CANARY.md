# Reading the automatic review evidence

This repository has an installed automatic review path for ordinary,
same-repository pull requests. It is evidence about one exact pull-request
head, not a merge decision or a claim that every finding is correct.

## Checks and review path

`Proof Check Pinned` checks the declared file scope for the exact head. Native
test jobs assess their own tests; neither result establishes that a model's
findings are true.

After the Owner marks a pull request Ready, `Claude Review` evaluates its exact
head. A trusted recorder then validates the Claude summary's provenance and its
head/run binding. If that succeeds, a separate Review App publishes a formal
`COMMENT` review for the same commit. The formal review is not an approval.

`Review Freshness` checks whether GitHub records a successful reviewer run for
the current head and whether the reviewer workflow matches the trusted copy. It
is not proof that the model findings are true.

The reviewer reports one of `CLEAN`, `ADVISORY`, or `REPAIR_REQUIRED` when it
has accepted structured review output. `REVIEW_EXECUTION_FAILED` means that
the reviewer execution or required evidence did not complete; it is distinct
from a substantive finding.

## Finding the public evidence

Readers can inspect the pull request's Actions runs for run summaries and
bounded diagnostics. The reviewer run also uploads the review-record artifact;
when publication is reached it uploads the formal-review-record artifact. The
pull request's formal `COMMENT` review is the public review published by the
Review App.

The current reviewer workflow triggers only for `opened` and
`ready_for_review`. Its Draft and fork guards exclude those pull requests, so a
push does not by itself start Claude Review. An installed or merged workflow is
not a successful live canary: qualification remains pending until the expected
evidence is observed on an ordinary pull request.

## What a later push does

A push moves the head. `Review Freshness` then looks for a successful reviewer
run recorded against the new head, finds none, and reports `NO_RUN_FOR_HEAD`,
so the check turns red. The earlier formal review stays visible on the pull
request and keeps its own `commit_id`, which is the previous head; it is
evidence about that commit and no longer about the one under review. Because
the reviewer triggers only on `opened` and `ready_for_review`, a new reviewer
run is requested by marking the pull request Ready again, not by pushing.
