# EXP-CONTAIN-001 roles

- Owner: `avoroncov971-maker`; merges the freeze and report and does not intervene during a run.
- Builder: this ChatGPT Builder session; builds the arms, scripts, and authored corpus; does not approve or merge its output.
- Held-out Builder: a fresh session that has not opened `src/proof_check/`, `docs/contract/`, or `fixtures/`; authors at least 18 hostile cases after freeze.
- Analyst: the Claude session that authored the protocol; adversarially reviews this Draft PR, verifies reveal hashes, and computes an independent summary.

Declared conflicts: the Builder implemented the GitHub Action prerequisite; the Analyst knows the Deedseal repositories and shares a vendor with Arm C if Arm C is later added by amendment. No role approves, certifies, or merges its own output.
