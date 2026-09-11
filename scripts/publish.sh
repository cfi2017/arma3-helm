#!/usr/bin/env bash
set -euo pipefail
repository="${GH_REPOSITORY,,}"
owner="${repository%%/*}"
version="$(helm show chart charts/arma3 | awk '/^version:/ {print $2}')"
registry="oci://ghcr.io/${repository}/charts"
# Check package tags through the authenticated API: a missing GHCR manifest
# can return a misleading authorization error before the first publication.
owner_type="$(gh api "repos/${GH_REPOSITORY}" --jq '.owner.type')"
owner_path=users
if [[ "$owner_type" == Organization ]]; then owner_path=orgs; fi
package="$(printf '%s' "${repository#*/}/charts/arma3" | jq -sRr @uri)"
versions_file="$(mktemp)"
error_file="$(mktemp)"
if gh api --paginate --slurp "${owner_path}/${owner}/packages/container/${package}/versions" >"$versions_file" 2>"$error_file"; then
  if jq -e --arg version "$version" '[.[][].metadata.container.tags[]] | index($version) != null' "$versions_file" >/dev/null; then
    echo "Chart ${version} already published; bump Chart.yaml version to release changes."
    exit 0
  fi
elif ! grep -q 'HTTP 404' "$error_file"; then
  cat "$error_file" >&2
  exit 1
fi
printf '%s' "$GH_TOKEN" | helm registry login ghcr.io --username "$GH_ACTOR" --password-stdin
trap 'helm registry logout ghcr.io' EXIT
mkdir -p dist
helm package charts/arma3 --destination dist
helm push "dist/arma3-${version}.tgz" "$registry"
