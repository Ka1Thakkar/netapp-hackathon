#!/usr/bin/env bash
#
# deploy_cloud_run.sh - Data in Motion (Hackathon) Cloud Run deployment helper.
#
# This script builds & deploys the Python FastAPI service and the Go mover
# service to Google Cloud Run, pushing container images to Artifact Registry.
# It can optionally create Pub/Sub topics that mirror the Kafka design.
#
# Features:
# - Argument parsing (project, region, repository, tags, memory/CPU, min instances)
# - Artifact Registry create (idempotent)
# - Docker build & push
# - Cloud Run deploy (python-api, go-mover)
# - Optional Pub/Sub topics creation
# - Basic validations (gcloud, docker)
#
# Usage:
#   ./deploy_cloud_run.sh \
#     --project my-project \
#     --region us-central1 \
#     --repo data-motion-repo \
#     --python-tag v0.1.0 \
#     --go-tag v0.1.0 \
#     --create-topics \
#     --allow-unauthenticated
#
# Run ./deploy_cloud_run.sh --help for all options.
#
# NOTE:
# - Requires Docker and gcloud CLI with auth configured.
# - SQLite persistence on Cloud Run is ephemeral; for production, adopt Cloud SQL.
# - For Pub/Sub swap-in, further code changes needed in services (not covered here).
#

set -euo pipefail

#######################################
# Configuration Defaults
#######################################
PROJECT_ID=""
REGION="us-central1"
REPO_NAME="data-motion-repo"
PY_SERVICE_NAME="python-api"
GO_SERVICE_NAME="go-mover"

PY_DOCKERFILE="deploy/docker/Dockerfile.python"
GO_DOCKERFILE="deploy/docker/Dockerfile.go"

PY_TAG="latest"
GO_TAG="latest"

CREATE_TOPICS=false
TOPICS=("access-events" "migration-jobs" "migration-status")

ALLOW_UNAUTHENTICATED=false
PY_MEMORY="512Mi"
GO_MEMORY="256Mi"
PY_CPU="1"
GO_CPU="1"
PY_MIN_INSTANCES="0"
GO_MIN_INSTANCES="0"
PY_MAX_INSTANCES="5"
GO_MAX_INSTANCES="5"

EXTRA_PY_ENV=()
EXTRA_GO_ENV=()

VERBOSE=false
DRY_RUN=false

#######################################
# Colors (fallback if not TTY)
#######################################
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'
  C_INFO=$'\033[1;34m'
  C_WARN=$'\033[1;33m'
  C_ERR=$'\033[1;31m'
  C_OK=$'\033[1;32m'
else
  C_RESET=""
  C_INFO=""
  C_WARN=""
  C_ERR=""
  C_OK=""
fi

#######################################
# Logging helpers
#######################################
log_info() { echo "${C_INFO}[INFO]${C_RESET} $*"; }
log_warn() { echo "${C_WARN}[WARN]${C_RESET} $*" >&2; }
log_err()  { echo "${C_ERR}[ERROR]${C_RESET} $*" >&2; }
log_ok()   { echo "${C_OK}[OK]${C_RESET} $*"; }
log_dbg()  { [[ "${VERBOSE}" == "true" ]] && echo "[DBG] $*"; }

die() { log_err "$*"; exit 1; }

#######################################
# Help / Usage
#######################################
usage() {
cat <<EOF
deploy_cloud_run.sh - Deploy Data in Motion services to Cloud Run.

Required:
  --project <id>                GCP project id
Optional:
  --region <region>             Region (default: ${REGION})
  --repo <name>                 Artifact Registry repo name (default: ${REPO_NAME})
  --python-tag <tag>            Python image tag (default: ${PY_TAG})
  --go-tag <tag>                Go image tag (default: ${GO_TAG})
  --py-service <name>           Cloud Run service name for Python (default: ${PY_SERVICE_NAME})
  --go-service <name>           Cloud Run service name for Go (default: ${GO_SERVICE_NAME})
  --create-topics               Create Pub/Sub topics (access-events, migration-jobs, migration-status)
  --allow-unauthenticated       Allow public ingress (useful for demo)
  --py-memory <mem>             Python service memory (default: ${PY_MEMORY})
  --go-memory <mem>             Go service memory (default: ${GO_MEMORY})
  --py-cpu <cpu>                Python service CPU (default: ${PY_CPU})
  --go-cpu <cpu>                Go service CPU (default: ${GO_CPU})
  --py-min-instances <n>        Python min instances (default: ${PY_MIN_INSTANCES})
  --go-min-instances <n>        Go min instances (default: ${GO_MIN_INSTANCES})
  --py-max-instances <n>        Python max instances (default: ${PY_MAX_INSTANCES})
  --go-max-instances <n>        Go max instances (default: ${GO_MAX_INSTANCES})
  --py-env K=V                  Extra env var for Python (repeatable)
  --go-env K=V                  Extra env var for Go (repeatable)
  --dry-run                     Show actions without executing
  --verbose                     Enable debug logging
  --help                        Show this help

Examples:
  ./deploy_cloud_run.sh --project my-proj --region us-central1 \\
    --python-tag v0.2.0 --go-tag v0.2.0 --create-topics \\
    --allow-unauthenticated --py-env LOG_LEVEL=DEBUG

EOF
}

#######################################
# Parse arguments
#######################################
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --project) PROJECT_ID="$2"; shift 2 ;;
      --region) REGION="$2"; shift 2 ;;
      --repo) REPO_NAME="$2"; shift 2 ;;
      --python-tag) PY_TAG="$2"; shift 2 ;;
      --go-tag) GO_TAG="$2"; shift 2 ;;
      --py-service) PY_SERVICE_NAME="$2"; shift 2 ;;
      --go-service) GO_SERVICE_NAME="$2"; shift 2 ;;
      --create-topics) CREATE_TOPICS=true; shift ;;
      --allow-unauthenticated) ALLOW_UNAUTHENTICATED=true; shift ;;
      --py-memory) PY_MEMORY="$2"; shift 2 ;;
      --go-memory) GO_MEMORY="$2"; shift 2 ;;
      --py-cpu) PY_CPU="$2"; shift 2 ;;
      --go-cpu) GO_CPU="$2"; shift 2 ;;
      --py-min-instances) PY_MIN_INSTANCES="$2"; shift 2 ;;
      --go-min-instances) GO_MIN_INSTANCES="$2"; shift 2 ;;
      --py-max-instances) PY_MAX_INSTANCES="$2"; shift 2 ;;
      --go-max-instances) GO_MAX_INSTANCES="$2"; shift 2 ;;
      --py-env)
          EXTRA_PY_ENV+=("$2")
          shift 2
          ;;
      --go-env)
          EXTRA_GO_ENV+=("$2")
          shift 2
          ;;
      --verbose) VERBOSE=true; shift ;;
      --dry-run) DRY_RUN=true; shift ;;
      --help|-h) usage; exit 0 ;;
      *)
        log_err "Unknown argument: $1"
        usage
        exit 1
        ;;
    esac
  done

  [[ -z "${PROJECT_ID}" ]] && die "--project is required"
}

#######################################
# Preconditions
#######################################
check_tools() {
  command -v gcloud >/dev/null 2>&1 || die "gcloud CLI not found"
  command -v docker  >/dev/null 2>&1 || die "docker not found"
}

#######################################
# Execute (with dry-run support)
#######################################
run() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[DRY-RUN] $*"
  else
    eval "$@"
  fi
}

#######################################
# Create Artifact Registry (idempotent)
#######################################
ensure_artifact_registry() {
  log_info "Ensuring Artifact Registry repository: ${REPO_NAME} in ${REGION}"
  if gcloud artifacts repositories describe "${REPO_NAME}" --location="${REGION}" >/dev/null 2>&1; then
    log_info "Repository ${REPO_NAME} already exists."
  else
    run "gcloud artifacts repositories create ${REPO_NAME} --repository-format=docker --location='${REGION}' --description='Data in Motion images'"
    log_ok "Created repository ${REPO_NAME}"
  fi
}

#######################################
# Build & push image
#######################################
build_and_push() {
  local svc="$1" dockerfile="$2" tag="$3"
  local image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${svc}:${tag}"

  log_info "Building image: ${image} (Dockerfile: ${dockerfile})"
  run "docker build -f ${dockerfile} -t ${image} ."
  log_info "Pushing image: ${image}"
  run "docker push ${image}"
}

#######################################
# Deploy Cloud Run service
#######################################
deploy_service() {
  local svc="$1" image="$2" memory="$3" cpu="$4" min_inst="$5" max_inst="$6" envs=("${@:7}")

  local auth_flag="--no-allow-unauthenticated"
  [[ "${ALLOW_UNAUTHENTICATED}" == "true" ]] && auth_flag="--allow-unauthenticated"

  local env_flags=()
  for kv in "${envs[@]}"; do
    env_flags+=( "--set-env-vars" "$kv" )
  done

  log_info "Deploying Cloud Run service: ${svc}"
  run "gcloud run deploy ${svc} \
      --image ${image} \
      --region ${REGION} \
      --project ${PROJECT_ID} \
      ${auth_flag} \
      --memory ${memory} \
      --cpu ${cpu} \
      --min-instances ${min_inst} \
      --max-instances ${max_inst} \
      ${env_flags[*]}"
  log_ok "Deployed service: ${svc}"
}

#######################################
# Create Pub/Sub topics (optional)
#######################################
create_topics() {
  if [[ "${CREATE_TOPICS}" != "true" ]]; then
    log_info "Skipping Pub/Sub topic creation (flag not set)."
    return
  fi

  log_info "Creating Pub/Sub topics (if missing)..."
  for t in "${TOPICS[@]}"; do
    if gcloud pubsub topics describe "${t}" >/dev/null 2>&1; then
      log_info "Topic '${t}' already exists."
    else
      run "gcloud pubsub topics create ${t}"
      log_ok "Created topic '${t}'"
    fi
  done
}

#######################################
# Main
#######################################
main() {
  parse_args "$@"
  check_tools

  log_info "Project: ${PROJECT_ID}, Region: ${REGION}, Repo: ${REPO_NAME}"
  [[ "${DRY_RUN}" == "true" ]] && log_warn "Running in dry-run mode; no changes will be applied."

  ensure_artifact_registry

  # Configure docker authentication for Artifact Registry (only if not in dry-run)
  if [[ "${DRY_RUN}" != "true" ]]; then
    log_info "Configuring docker auth for Artifact Registry..."
    gcloud auth configure-docker "${REGION}-docker.pkg.dev" >/dev/null
  fi

  # Build & push images
  build_and_push "${PY_SERVICE_NAME}" "${PY_DOCKERFILE}" "${PY_TAG}"
  build_and_push "${GO_SERVICE_NAME}" "${GO_DOCKERFILE}" "${GO_TAG}"

  local py_image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${PY_SERVICE_NAME}:${PY_TAG}"
  local go_image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${GO_SERVICE_NAME}:${GO_TAG}"

  # Default env for Python
  PY_ENV_DEFAULT=(
    "APP_NAME=data-in-motion-python-api"
    "APP_VERSION=${PY_TAG}"
    "LOG_LEVEL=INFO"
    "SQLITE_PATH=/data/metadata.db"
    "MODEL_DIR=/models"
  )

  # Default env for Go
  GO_ENV_DEFAULT=(
    "APP_NAME=data-in-motion-go-mover"
    "APP_VERSION=${GO_TAG}"
    "LOG_LEVEL=INFO"
    "DISABLE_KAFKA=true"
  )

  # Combine defaults with user-provided extra env
  PY_ENV_COMBINED=("${PY_ENV_DEFAULT[@]}" "${EXTRA_PY_ENV[@]}")
  GO_ENV_COMBINED=("${GO_ENV_DEFAULT[@]}" "${EXTRA_GO_ENV[@]}")

  deploy_service "${PY_SERVICE_NAME}" "${py_image}" "${PY_MEMORY}" "${PY_CPU}" "${PY_MIN_INSTANCES}" "${PY_MAX_INSTANCES}" "${PY_ENV_COMBINED[@]}"
  deploy_service "${GO_SERVICE_NAME}" "${go_image}" "${GO_MEMORY}" "${GO_CPU}" "${GO_MIN_INSTANCES}" "${GO_MAX_INSTANCES}" "${GO_ENV_COMBINED[@]}"

  create_topics

  log_ok "Deployment completed."
  log_info "Fetch service URLs:"
  log_info "  gcloud run services describe ${PY_SERVICE_NAME} --region ${REGION} --project ${PROJECT_ID} --format 'value(status.url)'"
  log_info "  gcloud run services describe ${GO_SERVICE_NAME} --region ${REGION} --project ${PROJECT_ID} --format 'value(status.url)'"
}

main "$@"
