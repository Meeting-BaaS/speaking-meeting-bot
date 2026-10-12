#!/usr/bin/env bash
set -euo pipefail

# The speaking-bot image lives in the API server registry namespace of each
# environment, next to api-server-v2 and connector-v2, so the chart's
# imagePullSecret (registry-secret) and the on-cluster allowlists need nothing new.
get_image_repo() {
  local service="${1:?service is required}"

  if [[ "$service" != "speaking-bot" ]]; then
    echo "[ERROR] Unknown service: $service (this repository builds only 'speaking-bot')" >&2
    exit 1
  fi

  case "${ENVIRON:-}" in
    preprod)
      echo "rg.fr-par.scw.cloud/baas-api-server-preprod"
      ;;
    prod)
      echo "rg.fr-par.scw.cloud/meeting-baas-prod-api-server"
      ;;
    *)
      echo "[ERROR] ENVIRON must be either 'preprod' or 'prod'. Current: '${ENVIRON:-empty}'" >&2
      exit 1
      ;;
  esac
}
