#!/usr/bin/env bash
#
# Ship the current working tree to ECS: build both images, push to ECR,
# register new task definition revisions, and roll the services.
#
# Assumes deploy/provision-ecs.sh has already run. This script only changes
# what a release changes — no networking, no IAM, no service creation.
#
# The GitHub Actions `deploy` workflow does the same thing; this is the local
# equivalent for when you want to ship without pushing a tag.
#
# Usage:
#   ./deploy/deploy-ecs.sh            # both
#   ./deploy/deploy-ecs.sh api        # one
#
# Set AWS_CLI to a container if the local CLI is broken — see provision-ecs.sh.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${CLUSTER:-travel-planner}"
PROJECT="travel-planner"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGETS="${*:-api ui}"

AWS_CLI="${AWS_CLI:-aws}"
aws_() { $AWS_CLI "$@" --region "$REGION"; }
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

ACCOUNT=$($AWS_CLI sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
TAG="$(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet || echo -dirty)"

ALB_DNS=$(aws_ elbv2 describe-load-balancers --names "$PROJECT-alb" \
  --query 'LoadBalancers[0].DNSName' --output text)
BASE_URL="http://$ALB_DNS"
say "Deploying $TAG to $BASE_URL"

aws_ ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"

for kind in $TARGETS; do
  repo="$PROJECT-$kind"
  image="$REGISTRY/$repo:$TAG"

  say "Building $kind"
  if [ "$kind" = "api" ]; then
    context="$ROOT"
    build_args=()
  else
    context="$ROOT/frontend"
    # A relative base URL, not the ALB hostname. The UI and API share an origin
    # behind the load balancer, so "/api" is correct from any hostname the ALB
    # ends up with — including a custom domain later — and the image does not
    # have to be rebuilt when the DNS name changes. NEXT_PUBLIC_ values are
    # compiled into the client bundle, so this cannot be fixed at runtime.
    build_args=(--build-arg "NEXT_PUBLIC_API_BASE_URL=/api")
  fi

  # linux/amd64 explicitly: building on an Apple Silicon machine otherwise
  # produces an arm64 image that Fargate refuses to start, and the task just
  # stops with "exec format error" buried in CloudWatch.
  #
  # ${build_args[@]+"${build_args[@]}"} rather than a plain "${build_args[@]}":
  # macOS ships bash 3.2, where expanding an empty array under `set -u` is an
  # "unbound variable" error. The api branch sets build_args=(), so the plain
  # form aborts the deploy before the first image is built. Bash 4.4 fixed
  # this, which is why CI (ubuntu) never saw it.
  docker buildx build --platform linux/amd64 ${build_args[@]+"${build_args[@]}"} \
    -t "$image" -t "$REGISTRY/$repo:latest" --push "$context"

  say "Rolling $kind"
  # __BASE_URL__ is a bare URL — the template supplies the quoting that makes
  # ALLOWED_ORIGINS a JSON array. See deploy/ecs/README.md.
  td=$(sed -e "s|__ACCOUNT__|$ACCOUNT|g" -e "s|__REGION__|$REGION|g" \
           -e "s|__IMAGE__|$image|g" -e "s|__BASE_URL__|$BASE_URL|g" \
           "$ROOT/deploy/ecs/task-def-$kind.json")
  arn=$(aws_ ecs register-task-definition --cli-input-json "$td" \
    --query 'taskDefinition.taskDefinitionArn' --output text)
  # --desired-count 1 explicitly, so deploying is also how you come back from
  # the teardown workflow's `stop` mode, which sets it to 0. Without it the
  # deploy reports success while nothing is actually running.
  aws_ ecs update-service --cluster "$CLUSTER" --service "$PROJECT-$kind" \
    --task-definition "$arn" --desired-count 1 >/dev/null
  echo "  $PROJECT-$kind -> ${arn##*/}"
done

say "Waiting for services to stabilise (this takes a few minutes)"
# shellcheck disable=SC2086
services=$(for k in $TARGETS; do echo "$PROJECT-$k"; done)
# shellcheck disable=SC2086
aws_ ecs wait services-stable --cluster "$CLUSTER" --services $services

say "Checking the deployed API"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/api/health" || true)
if [ "$code" = "200" ]; then
  echo "  $BASE_URL/api/health -> 200"
  echo "  UI at $BASE_URL"
else
  echo "  $BASE_URL/api/health -> ${code:-no response}"
  echo "  Logs: $AWS_CLI logs tail /ecs/$PROJECT-api --follow --region $REGION"
  exit 1
fi
