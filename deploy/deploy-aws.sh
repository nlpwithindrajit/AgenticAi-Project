#!/usr/bin/env bash
#
# Build both images, push them to Amazon ECR, and print the App Runner steps.
#
#   ./deploy/deploy-aws.sh
#
# Deliberately does NOT create or update App Runner services. Creating one
# starts billing and takes several minutes to reverse, so the irreversible part
# is left to you — this script gets the images to ECR, which is the tedious bit.
#
# Prerequisites, checked below:
#   - a working `aws` CLI with credentials and a region
#   - docker running
#
set -euo pipefail

AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
API_REPO="${API_REPO:-travel-planner-api}"
UI_REPO="${UI_REPO:-travel-planner-ui}"
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

# --- preflight ---------------------------------------------------------------
command -v docker >/dev/null || fail "docker not found."
docker info >/dev/null 2>&1 || fail "the docker daemon is not running."
command -v aws >/dev/null || fail "the aws CLI is not installed."

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  fail "the aws CLI cannot authenticate. Check:
  aws sts get-caller-identity

If that reports \"No module named 'cryptography'\", the Homebrew awscli install
is broken rather than your credentials — reinstall it:
  brew reinstall awscli"
fi

[ -n "$AWS_REGION" ] || fail "no AWS region. Set AWS_REGION or run: aws configure"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# The UI bakes the API URL into its client bundle at build time, so it cannot
# be discovered later — it has to be known now.
if [ -z "${API_URL:-}" ]; then
  fail "API_URL is required and must be the API's public URL.

On a first deploy the API service does not exist yet, so run this twice:
  1. API_URL=http://placeholder ./deploy/deploy-aws.sh   # push both images
  2. create the API App Runner service, note its URL
  3. API_URL=https://<api>.awsapprunner.com ./deploy/deploy-aws.sh
  4. create (or redeploy) the UI service

A NEXT_PUBLIC_ value is compiled into the JavaScript the browser downloads;
setting it as an App Runner environment variable afterwards has no effect."
fi

step "Account ${ACCOUNT_ID}, region ${AWS_REGION}, tag ${TAG}"

# --- repositories ------------------------------------------------------------
for repo in "$API_REPO" "$UI_REPO"; do
  if ! aws ecr describe-repositories --repository-names "$repo" \
      --region "$AWS_REGION" >/dev/null 2>&1; then
    step "Creating ECR repository ${repo}"
    aws ecr create-repository \
      --repository-name "$repo" \
      --region "$AWS_REGION" \
      --image-scanning-configuration scanOnPush=true >/dev/null
  fi
done

step "Logging Docker in to ${REGISTRY}"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

# --- build and push ----------------------------------------------------------
# linux/amd64 explicitly: App Runner does not run arm64 images, and building on
# an Apple Silicon Mac defaults to arm64. The failure appears as a service that
# never becomes healthy, with nothing useful in the logs.
step "Building the API image (linux/amd64)"
docker build --platform linux/amd64 \
  -t "${REGISTRY}/${API_REPO}:${TAG}" \
  -t "${REGISTRY}/${API_REPO}:latest" \
  "$here"

step "Building the UI image (linux/amd64), API_URL=${API_URL}"
docker build --platform linux/amd64 \
  --build-arg "NEXT_PUBLIC_API_BASE_URL=${API_URL}" \
  -t "${REGISTRY}/${UI_REPO}:${TAG}" \
  -t "${REGISTRY}/${UI_REPO}:latest" \
  "$here/frontend"

step "Pushing"
docker push "${REGISTRY}/${API_REPO}:${TAG}"
docker push "${REGISTRY}/${API_REPO}:latest"
docker push "${REGISTRY}/${UI_REPO}:${TAG}"
docker push "${REGISTRY}/${UI_REPO}:latest"

# --- what is left for a human ------------------------------------------------
cat <<EOF

$(printf '\033[32mImages pushed.\033[0m')

  API  ${REGISTRY}/${API_REPO}:${TAG}
  UI   ${REGISTRY}/${UI_REPO}:${TAG}

Create the two App Runner services (console, or copy the commands below).

API service
  port            8000
  health check    HTTP /health
  env             ENVIRONMENT=production
                  ALLOWED_ORIGINS=["https://<ui-url>"]   <- required, see below
                  AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET
                  ANTHROPIC_API_KEY
                  LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY

UI service
  port            3000
  health check    HTTP /

Two things bite on a first deploy:

  1. ALLOWED_ORIGINS must contain the UI's real URL. If it does not, the
     browser blocks every request and the UI looks broken while the API
     reports healthy — there is no server-side error to find.

  2. The UI image has ${API_URL} compiled into it. If that is not the API's
     final URL, rebuild and push the UI image again; an App Runner env var
     cannot fix it.

Secrets belong in AWS Secrets Manager rather than plain env vars — App Runner
can reference them directly.
EOF
