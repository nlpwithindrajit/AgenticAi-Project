#!/usr/bin/env bash
#
# Print a point-in-time health snapshot of the deployed travel planner from
# CloudWatch: traffic, latency, errors, target health, and per-service CPU and
# memory.
#
# Read-only. It creates nothing and changes nothing, so it is safe to run at
# any time, including mid-deploy.
#
# This is not a substitute for the Grafana dashboard (see
# docs/superpowers/specs/2026-08-16-grafana-cloud-infra-monitoring-design.md).
# It exists because the metrics are useful before that connection is wired,
# and because it answers "did CloudWatch actually receive anything?" — the
# question you want settled before blaming a dashboard for showing nothing.
#
# Usage:
#   ./deploy/monitoring-snapshot.sh [hours]     # default: last 3 hours
#
# Credentials come from the usual AWS environment variables or profile.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT:-travel-planner}"
CLUSTER="${CLUSTER:-travel-planner}"
HOURS="${1:-3}"

# 5-minute buckets: ALB metrics are published each minute, but a wider bucket
# keeps a quiet service from looking like a flatline of zeros.
PERIOD=300

START=$(date -u -d "-${HOURS} hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
  || date -u -v-"${HOURS}"H +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "=== travel planner — CloudWatch snapshot ==="
echo "region  $REGION"
echo "window  $START -> $END  (${HOURS}h, ${PERIOD}s buckets)"
echo

# The LoadBalancer dimension is the ARN suffix (app/<name>/<id>), not the name
# and not the DNS record. Deriving it beats hardcoding an id that changes
# whenever the load balancer is recreated.
ALB_ARN=$(aws elbv2 describe-load-balancers --names "$PROJECT-alb" \
  --region "$REGION" --query 'LoadBalancers[0].LoadBalancerArn' \
  --output text 2>/dev/null || true)

if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  echo "No load balancer named $PROJECT-alb in $REGION — nothing to report."
  echo "Run deploy/provision-ecs.sh first."
  exit 1
fi

ALB_DIM=${ALB_ARN#*:loadbalancer/}
echo "load balancer  $ALB_DIM"

tg_dim() {
  local arn
  arn=$(aws elbv2 describe-target-groups --names "$1" --region "$REGION" \
    --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)
  # The dimension is the ARN's trailing `targetgroup/<name>/<id>`, so strip
  # through the LAST colon (`##`), not the first (`#`). Getting this wrong
  # yields a syntactically valid query that silently returns no data rather
  # than an error — which reads as "the service is down" on a dashboard.
  [ -n "$arn" ] && [ "$arn" != "None" ] && echo "${arn##*:}" || echo ""
}
API_TG=$(tg_dim "$PROJECT-api-tg")
UI_TG=$(tg_dim "$PROJECT-ui-tg")

# One get-metric-data call for everything: fewer round trips, and every series
# is then guaranteed to cover the identical window.
query() {
  cat <<JSON
[
  {"Id":"req","Label":"requests","MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"RequestCount","Dimensions":[{"Name":"LoadBalancer","Value":"$ALB_DIM"}]},"Period":$PERIOD,"Stat":"Sum"}},
  {"Id":"lat_avg","Label":"latency avg (s)","MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"TargetResponseTime","Dimensions":[{"Name":"LoadBalancer","Value":"$ALB_DIM"}]},"Period":$PERIOD,"Stat":"Average"}},
  {"Id":"lat_p95","Label":"latency p95 (s)","MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"TargetResponseTime","Dimensions":[{"Name":"LoadBalancer","Value":"$ALB_DIM"}]},"Period":$PERIOD,"Stat":"p95"}},
  {"Id":"lat_max","Label":"latency max (s)","MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"TargetResponseTime","Dimensions":[{"Name":"LoadBalancer","Value":"$ALB_DIM"}]},"Period":$PERIOD,"Stat":"Maximum"}},
  {"Id":"c2xx","Label":"2xx","MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"HTTPCode_Target_2XX_Count","Dimensions":[{"Name":"LoadBalancer","Value":"$ALB_DIM"}]},"Period":$PERIOD,"Stat":"Sum"}},
  {"Id":"c4xx","Label":"4xx","MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"HTTPCode_Target_4XX_Count","Dimensions":[{"Name":"LoadBalancer","Value":"$ALB_DIM"}]},"Period":$PERIOD,"Stat":"Sum"}},
  {"Id":"c5xx","Label":"5xx (target)","MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"HTTPCode_Target_5XX_Count","Dimensions":[{"Name":"LoadBalancer","Value":"$ALB_DIM"}]},"Period":$PERIOD,"Stat":"Sum"}},
  {"Id":"elb5xx","Label":"5xx (elb)","MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"HTTPCode_ELB_5XX_Count","Dimensions":[{"Name":"LoadBalancer","Value":"$ALB_DIM"}]},"Period":$PERIOD,"Stat":"Sum"}},
  {"Id":"api_cpu","Label":"api cpu %","MetricStat":{"Metric":{"Namespace":"AWS/ECS","MetricName":"CPUUtilization","Dimensions":[{"Name":"ClusterName","Value":"$CLUSTER"},{"Name":"ServiceName","Value":"$PROJECT-api"}]},"Period":$PERIOD,"Stat":"Average"}},
  {"Id":"api_mem","Label":"api memory %","MetricStat":{"Metric":{"Namespace":"AWS/ECS","MetricName":"MemoryUtilization","Dimensions":[{"Name":"ClusterName","Value":"$CLUSTER"},{"Name":"ServiceName","Value":"$PROJECT-api"}]},"Period":$PERIOD,"Stat":"Average"}},
  {"Id":"ui_cpu","Label":"ui cpu %","MetricStat":{"Metric":{"Namespace":"AWS/ECS","MetricName":"CPUUtilization","Dimensions":[{"Name":"ClusterName","Value":"$CLUSTER"},{"Name":"ServiceName","Value":"$PROJECT-ui"}]},"Period":$PERIOD,"Stat":"Average"}},
  {"Id":"ui_mem","Label":"ui memory %","MetricStat":{"Metric":{"Namespace":"AWS/ECS","MetricName":"MemoryUtilization","Dimensions":[{"Name":"ClusterName","Value":"$CLUSTER"},{"Name":"ServiceName","Value":"$PROJECT-ui"}]},"Period":$PERIOD,"Stat":"Average"}}
]
JSON
}

OUT=$(mktemp)
query > "$OUT"

# HealthyHostCount is per target group, so it needs its own dimensions rather
# than riding along on the load-balancer-wide queries above.
if [ -n "$API_TG" ]; then
  python3 - "$OUT" "$ALB_DIM" "$API_TG" "$UI_TG" "$PERIOD" <<'PY'
import json, sys
path, alb, api_tg, ui_tg, period = sys.argv[1:6]
q = json.load(open(path))
for ident, label, tg in (("api_up", "api healthy hosts", api_tg),
                         ("ui_up", "ui healthy hosts", ui_tg)):
    if not tg:
        continue
    q.append({"Id": ident, "Label": label, "MetricStat": {"Metric": {
        "Namespace": "AWS/ApplicationELB", "MetricName": "HealthyHostCount",
        "Dimensions": [{"Name": "LoadBalancer", "Value": alb},
                       {"Name": "TargetGroup", "Value": tg}]},
        "Period": int(period), "Stat": "Minimum"}})
json.dump(q, open(path, "w"))
PY
fi

aws cloudwatch get-metric-data \
  --region "$REGION" \
  --start-time "$START" --end-time "$END" \
  --metric-data-queries "file://$OUT" \
  --output json > /tmp/metric-data.json

rm -f "$OUT"

python3 - /tmp/metric-data.json <<'PY'
import json, sys

data = json.load(open(sys.argv[1]))["MetricDataResults"]
COUNTERS = {"requests", "2xx", "4xx", "5xx (target)", "5xx (elb)"}

print()
print(f"{'metric':<22} {'latest':>10} {'min':>10} {'max':>10} {'total':>10}  pts")
print("-" * 78)
empty = []
for r in data:
    label, vals = r["Label"], r["Values"]
    if not vals:
        empty.append(label)
        print(f"{label:<22} {'no data':>10} {'-':>10} {'-':>10} {'-':>10}    0")
        continue
    # CloudWatch returns newest-first; the last element is the oldest bucket.
    latest, lo, hi, total = vals[0], min(vals), max(vals), sum(vals)
    tot = f"{total:,.0f}" if label in COUNTERS else "-"
    print(f"{label:<22} {latest:>10.2f} {lo:>10.2f} {hi:>10.2f} {tot:>10}  {len(vals)}")

print()
if empty:
    print("No data for:", ", ".join(empty))
    print("For a service with no traffic this is expected — ALB counters are")
    print("only published when requests actually arrive.")
PY

echo
echo "Raw JSON left at /tmp/metric-data.json"
