#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

# Dry-run the provider-agnostic scripts so a broken pipeline is caught on the
# PR, not on the first push to preprod. Same checks as the connector's
# validate-ci.sh for the one service this repository ships.

echo "[INFO] Validating CI scripts..."

echo "[INFO] Checking shell syntax..."
for script in .ci/scripts/*.sh .ci/scripts/git/*.sh .githooks/*; do
  bash -n "$script"
done

echo "[INFO] Testing preprod push context..."
preprod_context="$(GITHUB_EVENT_NAME=push GITHUB_REF_NAME=preprod GITHUB_REF_TYPE=branch .ci/scripts/resolve-pipeline-context.sh)"
printf '%s\n' "$preprod_context" | grep -Fqx 'environment=preprod'
printf '%s\n' "$preprod_context" | grep -Fqx 'build_matrix=["speaking-bot"]'
printf '%s\n' "$preprod_context" | grep -Fqx 'deploy_target=speaking-bot'
printf '%s\n' "$preprod_context" | grep -Fqx 'should_run=true'

echo "[INFO] Testing manual dispatch context..."
dispatch_context="$(GITHUB_EVENT_NAME=workflow_dispatch INPUT_ENVIRONMENT=preprod INPUT_SERVICE=speaking-bot INPUT_IMAGE_TAG=manual-tag .ci/scripts/resolve-pipeline-context.sh)"
printf '%s\n' "$dispatch_context" | grep -Fqx 'image_tag=manual-tag'
printf '%s\n' "$dispatch_context" | grep -Fqx 'deploy_target=speaking-bot'

echo "[INFO] Testing release tag context..."
release_tag_context="$(GITHUB_EVENT_NAME=push GITHUB_REF_NAME=v9.9.9 GITHUB_REF_TYPE=tag MAIN_BRANCH_REF=HEAD .ci/scripts/resolve-pipeline-context.sh)"
printf '%s\n' "$release_tag_context" | grep -Fqx 'environment=prod'
printf '%s\n' "$release_tag_context" | grep -Fqx 'build_matrix=["speaking-bot"]'
printf '%s\n' "$release_tag_context" | grep -Eq '^image_tag=[0-9]{4}-[0-9]{2}-[0-9]{2}-v9\.9\.9$'

echo "[INFO] Rejecting a manual prod dispatch..."
if GITHUB_EVENT_NAME=workflow_dispatch INPUT_ENVIRONMENT=prod INPUT_SERVICE=speaking-bot .ci/scripts/resolve-pipeline-context.sh >/dev/null 2>&1; then
  echo "[ERROR] Manual prod dispatch should be rejected" >&2
  exit 1
fi

echo "[INFO] Testing build and push dry-run..."
DRY_RUN=true ENVIRON=preprod .ci/scripts/build-and-push.sh --service speaking-bot --image-tag dry-run-tag >/dev/null
DRY_RUN=true ENVIRON=prod .ci/scripts/build-and-push.sh --service speaking-bot --image-tag dry-run-tag >/dev/null
rm -f .ci/speaking-bot.local-image .ci/speaking-bot.remote-image

echo "[INFO] Testing deploy dry-run..."
FAKE_DEPLOYMENT=".ci/tmp-validate"
rm -rf "$FAKE_DEPLOYMENT"
mkdir -p "$FAKE_DEPLOYMENT/deployment/helm-charts"
touch "$FAKE_DEPLOYMENT/deployment/helm-charts/baas_controller.sh" "$FAKE_DEPLOYMENT/deployment/kubeconfig.yaml"
deploy_output="$(DRY_RUN=true HOME="$ROOT_DIR/$FAKE_DEPLOYMENT/home" .ci/scripts/deploy.sh --service speaking-bot --environment preprod --image-tag dry-run-tag --deployment-dir "$FAKE_DEPLOYMENT/deployment")"
rm -rf "$FAKE_DEPLOYMENT"
printf '%s\n' "$deploy_output" | grep -Fq 'speaking-bot upgrade'

echo "[INFO] Testing branch name and PR target policy..."
BRANCH_NAME=preprod PR_BASE_BRANCH=main GITHUB_EVENT_NAME=pull_request bash .ci/scripts/git/validate-branch-name.sh >/dev/null
BRANCH_NAME=feat/x PR_BASE_BRANCH=preprod GITHUB_EVENT_NAME=pull_request bash .ci/scripts/git/validate-branch-name.sh >/dev/null
if BRANCH_NAME=feat/x PR_BASE_BRANCH=main GITHUB_EVENT_NAME=pull_request bash .ci/scripts/git/validate-branch-name.sh >/dev/null 2>&1; then
  echo "[ERROR] A feature branch must not target main directly" >&2
  exit 1
fi
BRANCH_NAME=feat/valid-branch GITHUB_EVENT_NAME=push GITHUB_REF_TYPE=branch bash .ci/scripts/git/validate-branch-name.sh >/dev/null
if BRANCH_NAME=Bad_Branch GITHUB_EVENT_NAME=push GITHUB_REF_TYPE=branch bash .ci/scripts/git/validate-branch-name.sh >/dev/null 2>&1; then
  echo "[ERROR] Invalid branch name should be rejected" >&2
  exit 1
fi

echo "[SUCCESS] CI scripts validated"
