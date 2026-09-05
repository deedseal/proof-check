#!/usr/bin/env bash
set -euo pipefail

refuse() {
  printf 'REFUSED: %s\n' "$1" >&2
  exit 2
}

single_line() {
  [[ "$2" != *$'\n'* && "$2" != *$'\r'* ]] || refuse "$1 must contain one line"
}

action_ref="${GITHUB_ACTION_REF:-}"
if [[ -n "$action_ref" ]]; then
  source_commit="$action_ref"
else
  # A repository-local `uses: ./` does not provide GITHUB_ACTION_REF. It may
  # use GITHUB_SHA only when GitHub identifies no remote action repository and
  # the action directory is exactly the workflow workspace.
  [[ -z "${GITHUB_ACTION_REPOSITORY:-}" ]] || \
    refuse "action reference must be a full 40-hex commit; tags and branches are not pins"
  action_path="$(cd "${GITHUB_ACTION_PATH:-/nonexistent}" 2>/dev/null && pwd -P)" || \
    refuse "local action path is unavailable"
  workspace="$(cd "${GITHUB_WORKSPACE:-/nonexistent}" 2>/dev/null && pwd -P)" || \
    refuse "workflow workspace is unavailable"
  [[ "$action_path" == "$workspace" ]] || refuse "local action path must equal the workflow workspace"
  source_commit="${GITHUB_SHA:-}"
fi

[[ "$source_commit" =~ ^[0-9a-fA-F]{40}$ ]] || \
  refuse "action reference must be a full 40-hex commit; tags and branches are not pins"
source_commit="${source_commit,,}"

policy="${PROOF_CHECK_POLICY:-}"
receipt="${PROOF_CHECK_RECEIPT:-proof-check-receipt.json}"
target="${PROOF_CHECK_TARGET:-head}"
declaration="${PROOF_CHECK_DECLARATION:-}"
pr_number="${PROOF_CHECK_PR:-}"
repository="${PROOF_CHECK_REPO:-}"
token="${PROOF_CHECK_GITHUB_TOKEN:-}"
unset PROOF_CHECK_GITHUB_TOKEN

for pair in \
  "policy:$policy" \
  "receipt:$receipt" \
  "target:$target" \
  "declaration:$declaration" \
  "pr:$pr_number" \
  "repo:$repository"; do
  single_line "${pair%%:*}" "${pair#*:}"
done
[[ -n "$pr_number" ]] || refuse "pull request number is empty"
if [[ "${GITHUB_EVENT_NAME:-}" == "merge_group" || "$target" == "merge_group" ]]; then
  refuse "merge_group target requires GitHub event context"
fi
[[ "$target" == "head" || "$target" == "test_merge" ]] || refuse "target must be head or test_merge"

temporary_root="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/proof-check.XXXXXX")"
trap 'rm -rf -- "$temporary_root"' EXIT
checkout="$temporary_root/source"
venv="$temporary_root/venv"

git clone --quiet --filter=blob:none --no-checkout --depth 1 \
  https://github.com/deedseal/proof-check "$checkout"
git -C "$checkout" fetch --quiet --depth 1 origin "$source_commit"
[[ "$(git -C "$checkout" rev-parse FETCH_HEAD)" == "$source_commit" ]] || \
  refuse "fetched source does not match the pinned commit"
git -C "$checkout" checkout --quiet --detach "$source_commit"
[[ "$(git -C "$checkout" rev-parse HEAD)" == "$source_commit" ]] || \
  refuse "detached checkout does not match the pinned commit"

python3 -m venv "$venv"
"$venv/bin/python" -m pip install --disable-pip-version-check "$checkout"

command=(
  "$venv/bin/proof-check" check
  --repo "$repository"
  --pr "$pr_number"
  --policy "$policy"
  --receipt "$receipt"
  --target "$target"
  --token-env PROOF_CHECK_GITHUB_TOKEN
)
if [[ -n "$declaration" ]]; then
  command+=(--declaration "$declaration")
fi

set +e
PROOF_CHECK_GITHUB_TOKEN="$token" "${command[@]}"
exit_code=$?
set -e
unset token

verdict=""
reason_codes=""
receipt_source=""
receipt_digest=""
if [[ -f "$receipt" ]]; then
  receipt_digest="$(sha256sum -- "$receipt" | cut -d ' ' -f 1)"
  receipt_fields="$("$venv/bin/python" -c '
import json, sys
document = json.load(open(sys.argv[1], "rb"))
verdict = document.get("verdict", "")
reasons = document.get("reason_codes", [])
source = document.get("tool", {}).get("source_commit", "")
print(verdict if isinstance(verdict, str) else "")
print(", ".join(reasons) if isinstance(reasons, list) and all(isinstance(x, str) for x in reasons) else "")
print(source if isinstance(source, str) else "")
' "$receipt" 2>/dev/null || true)"
  mapfile -t fields <<< "$receipt_fields"
  verdict="${fields[0]:-}"
  reason_codes="${fields[1]:-}"
  receipt_source="${fields[2]:-}"
fi

output_file="${GITHUB_OUTPUT:-/dev/null}"
{
  printf 'verdict=%s\n' "$verdict"
  printf 'exit-code=%s\n' "$exit_code"
  printf 'receipt-path=%s\n' "$receipt"
  printf 'receipt-digest=%s\n' "$receipt_digest"
} >> "$output_file"

summary_file="${GITHUB_STEP_SUMMARY:-/dev/null}"
{
  printf '### Proof Check\n\n'
  printf -- '- Verdict: `%s`\n' "${verdict:-not recorded}"
  printf -- '- Reason codes: `%s`\n' "${reason_codes:-none recorded}"
  printf -- '- Receipt digest: `%s`\n' "${receipt_digest:-not recorded}"
  printf -- '- Source commit: `%s`\n' "${receipt_source:-$source_commit}"
} >> "$summary_file"

exit "$exit_code"
