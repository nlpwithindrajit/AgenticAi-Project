#!/usr/bin/env bash
#
# One-time AWS provisioning for the travel planner on ECS Fargate.
#
# Creates everything that does not change between deploys: ECR repositories, a
# cluster, an Application Load Balancer with two target groups, IAM, log groups,
# SSM parameters for the secrets, and the two services. Shipping new code
# afterwards is `deploy/deploy-ecs.sh` or the `deploy` GitHub Actions workflow —
# neither of which touches any of this.
#
# Idempotent: every resource is looked up before it is created, so re-running
# after a partial failure resumes rather than duplicating.
#
# WHY ONE LOAD BALANCER, NOT TWO
# ------------------------------
# The UI is served at / and the API at /api/*, from a single origin. That costs
# one ALB instead of two, and — more importantly — the browser never makes a
# cross-origin request, so CORS cannot silently break the deployed site. The
# API answers on both / and /api/* (see app/main.py), so local development and
# the test suite are unaffected.
#
# Usage:
#   ./deploy/provision-ecs.sh
#
# Credentials come from the usual AWS environment variables or profile. If the
# local `aws` CLI is broken (a Homebrew awscli missing `cryptography` is common
# on macOS), set AWS_CLI to a container instead:
#
#   export AWS_CLI="docker run --rm --env-file /path/to/awsenv amazon/aws-cli"

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${CLUSTER:-travel-planner}"
PROJECT="travel-planner"
API_REPO="$PROJECT-api"
UI_REPO="$PROJECT-ui"
EXEC_ROLE="travelPlannerEcsExecutionRole"

AWS_CLI="${AWS_CLI:-aws}"
aws_() { $AWS_CLI "$@" --region "$REGION"; }

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
have() { [ -n "$1" ] && [ "$1" != "None" ]; }

ACCOUNT=$($AWS_CLI sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
say "Account $ACCOUNT, region $REGION"

# --- ECR ---------------------------------------------------------------
for repo in "$API_REPO" "$UI_REPO"; do
  if aws_ ecr describe-repositories --repository-names "$repo" >/dev/null 2>&1; then
    echo "ECR $repo exists"
  else
    aws_ ecr create-repository --repository-name "$repo" \
      --image-scanning-configuration scanOnPush=true >/dev/null
    echo "ECR $repo created"
  fi
done

# --- Secrets -----------------------------------------------------------
# Stored as SSM SecureString and injected by the ECS agent at task start, so no
# key is ever written into a task definition (task definitions are readable by
# anyone with ecs:DescribeTaskDefinition, and every revision is kept forever).
say "SSM parameters"
put_secret() {
  local name="$1" value="$2"
  if [ -z "$value" ]; then
    if aws_ ssm get-parameter --name "/$PROJECT/$name" >/dev/null 2>&1; then
      echo "  /$PROJECT/$name kept (already set)"
    else
      echo "  /$PROJECT/$name MISSING — export $name and re-run, or set it in"
      echo "  the console. The API will start but that feature will be off."
    fi
    return
  fi
  aws_ ssm put-parameter --name "/$PROJECT/$name" --type SecureString \
    --value "$value" --overwrite >/dev/null
  echo "  /$PROJECT/$name set"
}
put_secret OPENAI_API_KEY "${OPENAI_API_KEY:-}"
put_secret LANGFUSE_PUBLIC_KEY "${LANGFUSE_PUBLIC_KEY:-}"
put_secret LANGFUSE_SECRET_KEY "${LANGFUSE_SECRET_KEY:-}"

# --- IAM ---------------------------------------------------------------
say "Execution role"
if aws_ iam get-role --role-name "$EXEC_ROLE" >/dev/null 2>&1; then
  echo "  $EXEC_ROLE exists"
else
  $AWS_CLI iam create-role --role-name "$EXEC_ROLE" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }' >/dev/null
  echo "  $EXEC_ROLE created"
fi
$AWS_CLI iam attach-role-policy --role-name "$EXEC_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
# The managed policy covers ECR and CloudWatch but not SSM, so the secrets
# above need this. Without it tasks fail at start with ResourceInitializationError.
$AWS_CLI iam put-role-policy --role-name "$EXEC_ROLE" \
  --policy-name ReadTravelPlannerSecrets \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [\"ssm:GetParameters\", \"kms:Decrypt\"],
      \"Resource\": [
        \"arn:aws:ssm:$REGION:$ACCOUNT:parameter/$PROJECT/*\",
        \"arn:aws:kms:$REGION:$ACCOUNT:alias/aws/ssm\"
      ]
    }]
  }"
echo "  policies attached"

# --- Logs --------------------------------------------------------------
for group in "/ecs/$API_REPO" "/ecs/$UI_REPO"; do
  aws_ logs create-log-group --log-group-name "$group" 2>/dev/null || true
  aws_ logs put-retention-policy --log-group-name "$group" --retention-in-days 14
done
echo "log groups ready (14 day retention)"

# --- Network -----------------------------------------------------------
say "Network"
VPC=$(aws_ ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
have "$VPC" || { echo "No default VPC in $REGION. Create one or set VPC by hand."; exit 1; }

# Public subnets only: Fargate tasks need a route to the internet to pull the
# image from ECR, and a public subnet with a public IP avoids paying for a NAT.
SUBNETS=$(aws_ ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC" "Name=map-public-ip-on-launch,Values=true" \
  --query 'Subnets[].SubnetId' --output text)
have "$SUBNETS" || { echo "No public subnets in $VPC."; exit 1; }
SUBNET_CSV=$(echo "$SUBNETS" | tr '\t' ',')
echo "  vpc $VPC"
echo "  subnets $SUBNET_CSV"

sg_id() {
  aws_ ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=$VPC" "Name=group-name,Values=$1" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null
}
ensure_sg() {
  local name="$1" desc="$2" id
  id=$(sg_id "$name")
  if ! have "$id"; then
    id=$(aws_ ec2 create-security-group --group-name "$name" \
      --description "$desc" --vpc-id "$VPC" --query GroupId --output text)
  fi
  echo "$id"
}
ALB_SG=$(ensure_sg "$PROJECT-alb-sg" "Public HTTP for the travel planner ALB")
TASK_SG=$(ensure_sg "$PROJECT-task-sg" "Travel planner Fargate tasks")
echo "  alb sg $ALB_SG, task sg $TASK_SG"

# `|| true` throughout: the duplicate-rule error is the success case on a re-run.
aws_ ec2 authorize-security-group-ingress --group-id "$ALB_SG" \
  --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
# Tasks accept traffic only from the load balancer, never from the internet,
# even though they sit in a public subnet with a public IP.
for port in 8000 3000; do
  aws_ ec2 authorize-security-group-ingress --group-id "$TASK_SG" \
    --protocol tcp --port "$port" --source-group "$ALB_SG" >/dev/null 2>&1 || true
done

# --- Load balancer -----------------------------------------------------
say "Load balancer"
ALB_ARN=$(aws_ elbv2 describe-load-balancers --names "$PROJECT-alb" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)
if ! have "$ALB_ARN"; then
  # shellcheck disable=SC2086
  ALB_ARN=$(aws_ elbv2 create-load-balancer --name "$PROJECT-alb" \
    --type application --scheme internet-facing \
    --subnets $SUBNETS --security-groups "$ALB_SG" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)
  echo "  created"
fi
ALB_DNS=$(aws_ elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].DNSName' --output text)
BASE_URL="http://$ALB_DNS"
echo "  $BASE_URL"

ensure_tg() {
  local name="$1" port="$2" hc="$3" arn
  arn=$(aws_ elbv2 describe-target-groups --names "$name" \
    --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)
  if ! have "$arn"; then
    # target-type ip is required for awsvpc/Fargate — instance targets do not
    # exist here, and the service create fails late and unhelpfully without it.
    arn=$(aws_ elbv2 create-target-group --name "$name" --protocol HTTP \
      --port "$port" --vpc-id "$VPC" --target-type ip \
      --health-check-path "$hc" --health-check-interval-seconds 30 \
      --healthy-threshold-count 2 --unhealthy-threshold-count 3 \
      --query 'TargetGroups[0].TargetGroupArn' --output text)
  fi
  # 30s instead of the 300s default: a rolling deploy otherwise spends five
  # extra minutes draining a container that has already stopped serving.
  aws_ elbv2 modify-target-group-attributes --target-group-arn "$arn" \
    --attributes Key=deregistration_delay.timeout_seconds,Value=30 >/dev/null
  echo "$arn"
}
API_TG=$(ensure_tg "$PROJECT-api-tg" 8000 /api/health)
UI_TG=$(ensure_tg "$PROJECT-ui-tg" 3000 /)

LISTENER=$(aws_ elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
  --query 'Listeners[?Port==`80`]|[0].ListenerArn' --output text 2>/dev/null || true)
if ! have "$LISTENER"; then
  LISTENER=$(aws_ elbv2 create-listener --load-balancer-arn "$ALB_ARN" \
    --protocol HTTP --port 80 \
    --default-actions "Type=forward,TargetGroupArn=$UI_TG" \
    --query 'Listeners[0].ListenerArn' --output text)
fi
# Everything not matching /api/* falls through to the UI via the default action.
if ! aws_ elbv2 describe-rules --listener-arn "$LISTENER" \
  --query 'Rules[?Priority==`10`]' --output text | grep -q .; then
  aws_ elbv2 create-rule --listener-arn "$LISTENER" --priority 10 \
    --conditions 'Field=path-pattern,Values=/api/*' \
    --actions "Type=forward,TargetGroupArn=$API_TG" >/dev/null
fi
echo "  / -> ui, /api/* -> api"

# --- Cluster and services ---------------------------------------------
say "Cluster and services"
aws_ ecs create-cluster --cluster-name "$CLUSTER" >/dev/null 2>&1 || true

# __BASE_URL__ is substituted as a bare URL. The quoting that turns it into
# ALLOWED_ORIGINS' JSON array lives in the template — see deploy/ecs/README.md.
render() {
  sed -e "s|__ACCOUNT__|$ACCOUNT|g" -e "s|__REGION__|$REGION|g" \
      -e "s|__IMAGE__|$2|g" -e "s|__BASE_URL__|$3|g" "$1"
}

# A placeholder image so the service can be created before the first real
# build. The deploy step replaces it immediately.
API_IMAGE="$REGISTRY/$API_REPO:latest"
UI_IMAGE="$REGISTRY/$UI_REPO:latest"

# Split on '|' rather than ':' — both the ECR image reference and the target
# group ARN contain colons, so a colon-delimited record silently mis-parses.
for spec in "api|$API_IMAGE|$API_TG|8000" "ui|$UI_IMAGE|$UI_TG|3000"; do
  IFS='|' read -r kind image tg port <<<"$spec"
  family="$PROJECT-$kind"
  td=$(render "$(dirname "$0")/ecs/task-def-$kind.json" "$image" "$BASE_URL")
  arn=$(aws_ ecs register-task-definition --cli-input-json "$td" \
    --query 'taskDefinition.taskDefinitionArn' --output text)
  echo "  registered $arn"

  status=$(aws_ ecs describe-services --cluster "$CLUSTER" --services "$family" \
    --query 'services[0].status' --output text 2>/dev/null || true)
  if [ "$status" = "ACTIVE" ]; then
    aws_ ecs update-service --cluster "$CLUSTER" --service "$family" \
      --task-definition "$arn" >/dev/null
    echo "  updated service $family"
  else
    aws_ ecs create-service --cluster "$CLUSTER" --service-name "$family" \
      --task-definition "$arn" --desired-count 1 --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_CSV],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}" \
      --load-balancers "targetGroupArn=$tg,containerName=$kind,containerPort=$port" \
      --health-check-grace-period-seconds 90 \
      --deployment-configuration "maximumPercent=200,minimumHealthyPercent=100" \
      >/dev/null
    echo "  created service $family"
  fi
done

say "Done"
cat <<EOF
  URL       $BASE_URL
  UI        $BASE_URL/
  API       $BASE_URL/api/health

Add these as GitHub repository secrets/variables so the deploy workflow works:
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   (or an OIDC role — see README)

Tasks will not be healthy until real images are pushed:
  ./deploy/deploy-ecs.sh
EOF
