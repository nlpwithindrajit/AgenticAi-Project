# Grafana Cloud infrastructure monitoring — design

**Date:** 2026-08-16
**Status:** approved, ready for implementation planning
**Scope:** slice 1 of 4 (infrastructure health only)

## Context

The travel planner runs on ECS Fargate behind one ALB in AWS account
`024899754281`. Langfuse traces the LLM and agent layer — one trace per
request, a span per agent — but nothing watches the infrastructure. There is no
dashboard, no alerting, and no way to answer "is it up, how fast is it, is it
erroring" without opening the AWS console and clicking through CloudWatch.

The application emits **no custom metrics at all**: no CloudWatch metrics, no
Prometheus, no instrumentation in `app/`. Everything CloudWatch knows about
this system today is what ALB and ECS publish on their own.

This slice adds a Grafana Cloud dashboard over those existing metrics. It
changes no application code.

## Decisions

Four monitoring areas were identified (infra health, application/business
metrics, AWS cost, LLM cost). They are independent subsystems with very
different effort, so they are being built in sequence rather than as one
project. **This spec covers infra health only.** The rest are recorded under
"Future slices" so the boundary is explicit.

### Grafana Cloud free tier, not self-hosted or managed

Verified 2026-08-16: the free tier gives 10,000 active metric series, 14-day
retention, 3 users, no credit card, and does not expire. That is comfortably
more than this slice needs, and 14 days happens to match the existing 14-day
CloudWatch log retention, so neither side becomes the shorter horizon by
surprise.

Rejected: Amazon Managed Grafana bills $9/editor/month and requires IAM
Identity Center or SAML, which is real setup work for a one-person project.
Self-hosting Grafana as a third ECS service would fit the repo's one-ALB
pattern well but costs ~$10-15/month of Fargate for a dashboard nobody is
watching most of the time.

### Pull from CloudWatch, do not push metrics

Grafana Cloud *queries* CloudWatch rather than ingesting from it. This matters
for cost in two ways, and both point the same direction:

- Queried metrics are not ingested series, so they do not consume any of the
  10,000-series free-tier budget. The Grafana side of this slice is genuinely
  $0, not "free until it isn't".
- Routing metrics *into* CloudWatch as custom metrics would bill $0.30 per
  metric per month before Grafana ever saw them.

The only charge is CloudWatch `GetMetricData` at roughly $0.01 per 1,000
metrics requested — pennies per month, under $1 even with a dashboard tab left
open continuously.

This decision is specific to slice 1, where every metric already exists in
CloudWatch. Slice 2 reverses it: application metrics have no reason to pass
through CloudWatch and should push to Grafana Cloud via Prometheus
`remote_write` instead. Fargate tasks sit behind the ALB, so Grafana Cloud
cannot scrape them, and exposing `/metrics` publicly to make scraping possible
would be the wrong trade.

### Assume-role authentication, no stored keys

Grafana Cloud authenticates by having its own AWS account assume a role in
ours, via STS, gated by an External ID unique to our Grafana org. No access
key is created, stored, or rotated. The External ID is what stops another
Grafana Cloud customer from naming our role and reading our metrics.

### No Container Insights

Container Insights is **off** on the cluster — `provision-ecs.sh` calls
`ecs create-cluster` with no `--settings containerInsights=enabled` — and
enabling it bills per published metric.

The consequence is that task-count metrics (`RunningTaskCount`,
`DesiredTaskCount`) are unavailable, since those are Container Insights
metrics rather than `AWS/ECS` built-ins. Cluster-level utilisation metrics are
also unavailable, but only for EC2-backed clusters, so this costs us nothing.

Service-level `CPUUtilization` and `MemoryUtilization` **are** published
automatically for Fargate services in the `AWS/ECS` namespace, which is what
the dashboard uses.

For liveness, ALB `HealthyHostCount` is used instead of a task count. It is
the better signal regardless of cost: it reports whether a task is actually
passing its health check and receiving traffic, not merely whether ECS
believes it is running.

## Architecture

```
Grafana Cloud (grafana.net)
      │
      │  sts:AssumeRole  +  ExternalID
      ▼
IAM role  travelPlannerGrafanaReadOnly        account 024899754281
      │
      │  read-only
      ▼
CloudWatch  ◀── ALB and ECS publish automatically; no agent, no sidecar
```

Nothing is deployed into the VPC. No collector, no exporter, no new container.
The only artefact created in AWS is one IAM role.

## Existing resources this depends on

Confirmed by reading `deploy/provision-ecs.sh`:

| Resource | Name |
|---|---|
| Region / account | `us-east-1` / `024899754281` |
| Load balancer | `travel-planner-alb` |
| Target groups | `travel-planner-api-tg`, `travel-planner-ui-tg` |
| ECS cluster | `travel-planner` |
| ECS services | `travel-planner-api`, `travel-planner-ui` |
| Log groups | `/ecs/travel-planner-api`, `/ecs/travel-planner-ui` (14-day retention) |

## Components

### 1. `deploy/grafana/trust-policy.json`

Trust policy templated on `__GRAFANA_ACCOUNT_ID__` and `__EXTERNAL_ID__`, both
supplied by the Grafana Cloud UI at datasource-setup time. Conditions the
`sts:AssumeRole` on `sts:ExternalId` matching exactly.

### 2. `deploy/grafana/readonly-policy.json`

Permission policy. Read-only by construction:

- `cloudwatch:DescribeAlarmsForMetric`, `DescribeAlarmHistory`, `DescribeAlarms`
- `cloudwatch:ListMetrics`, `GetMetricData`, `GetMetricStatistics`
- `logs:DescribeLogGroups`, `DescribeLogStreams`, `GetLogEvents`,
  `StartQuery`, `StopQuery`, `GetQueryResults`, `GetLogGroupFields`
- `tag:GetResources` — how the CloudWatch datasource resolves dimension names
- `ec2:DescribeRegions` — region list in the datasource UI

No wildcard action, no `Put*`, `Delete*`, `Create*`, or `Update*`.

### 3. `.github/workflows/provision-grafana.yml`

A `workflow_dispatch` workflow taking `grafana_account_id` and `external_id` as
inputs. It renders the two policy documents, creates the role if absent or
updates the trust and permission policies if present, and prints the role ARN
in the job summary.

This exists for the same reason `sync-secrets.yml` does: no local AWS profile
authenticates to account `024899754281`, so anything that must be created
there has to be created by CI. It follows the established conventions — the
`deploy-production` concurrency group and `environment: production`.

It deliberately does **not** live in `provision-ecs.sh`. That script builds the
running system; this role is observability wiring that can be created,
destroyed, and re-created without touching the service.

### 4. `deploy/grafana/travel-planner-infra.json`

The dashboard, version-controlled and imported through the Grafana UI.
Grafana's export format carries a `__inputs` datasource placeholder, so the
file is portable across workspaces rather than pinned to one datasource UID.

## Dashboard panels

All metrics below were confirmed to be published without additional
configuration.

**Traffic** — namespace `AWS/ApplicationELB`, dimension
`LoadBalancer=app/travel-planner-alb/*`

- `RequestCount` (Sum)
- `TargetResponseTime` — p50, p95, p99 via extended statistics

**Errors** — same namespace and dimension

- `HTTPCode_Target_5XX_Count` (Sum) — the application failed
- `HTTPCode_ELB_5XX_Count` (Sum) — the load balancer failed before reaching a
  task. Kept as a separate series: the two have completely different causes and
  summing them hides which one is happening.
- `HTTPCode_Target_4XX_Count` (Sum)

**Availability** — dimensions `LoadBalancer` + `TargetGroup`, one series per
target group

- `HealthyHostCount` (Minimum — the worst point in the interval, not an average
  that smooths a brief outage away)
- `UnHealthyHostCount` (Maximum)

**Resources** — namespace `AWS/ECS`, dimensions `ClusterName=travel-planner`,
`ServiceName=travel-planner-api|travel-planner-ui`

- `CPUUtilization` (Average and Maximum)
- `MemoryUtilization` (Average and Maximum)

**Logs** — CloudWatch Logs Insights over `/ecs/travel-planner-api`

- Recent `ERROR`/`Traceback` lines, most recent first

## Testing

Extends `tests/test_deploy_manifests.py`, which already validates deployment
artefacts.

1. Both policy documents are valid JSON and, once rendered, contain no
   `__PLACEHOLDER__` left behind.

   Scope this check to the **policy** files only. The dashboard JSON keeps
   Grafana's own `__inputs` / `${DS_...}` placeholders on purpose — that is
   what makes it importable into any workspace instead of pinned to one
   datasource UID. A blanket "no placeholders under `deploy/grafana/`" test
   would fail on a correct dashboard.
2. **The permission policy grants nothing mutating.** Every action matches a
   read-only prefix (`Get`, `List`, `Describe`, `Start`, `Stop`), and no action
   is `*` or ends in `:*`. This is the assertion worth having: a monitoring
   role that can write is easy to introduce later by pasting a broader policy,
   and nothing else in the system would notice.
3. The trust policy conditions on `sts:ExternalId`. Without that condition the
   role is assumable by any Grafana Cloud tenant, which looks identical in the
   console and in every dashboard.
4. The dashboard references only namespaces the deployment actually publishes
   (`AWS/ApplicationELB`, `AWS/ECS`) and no Container Insights namespace.
5. `provision-grafana.yml` is valid YAML, is `workflow_dispatch`-only, and
   shares the `deploy-production` concurrency group.

Verification is a live check: run the workflow, confirm `aws iam get-role`
returns the role with the external-id condition, then confirm the datasource
test passes in Grafana and panels render real data.

## Manual steps

Two things cannot be automated from here.

1. **Create the Grafana Cloud account.** Sign up for the free tier, add a
   CloudWatch datasource, and choose the "Grafana assume role" auth method. The
   setup page shows a **Grafana AWS Account ID** and an **External ID**; both
   are needed as workflow inputs.
2. **Paste the role ARN back.** After the workflow prints it, enter it in the
   datasource and click *Test*.

## Failure modes

| Symptom | Cause | Resolution |
|---|---|---|
| Datasource test returns `AccessDenied` | Role ARN wrong, or external ID mismatch | Re-run the workflow with the correct external ID; it updates in place |
| Panels render empty but the test passes | Wrong region selected in the datasource | Set it to `us-east-1` |
| `HealthyHostCount` has no data | Target group name changed | Dashboard dimensions are wildcards on the ALB but exact on target groups; update the JSON |
| Role already exists | Re-running the workflow | Expected — it updates the policies rather than failing, matching `provision-ecs.sh` |

## Teardown

The IAM role is intentionally **not** added to `teardown.yml`'s destroy path.
It is not cluster infrastructure, it costs nothing to keep, and deleting it
means redoing the Grafana-side wiring by hand. Tearing down the cluster and
rebuilding it later leaves the monitoring connection intact.

## Cost

| Item | Cost |
|---|---|
| Grafana Cloud free tier | $0 |
| IAM role | $0 |
| CloudWatch `GetMetricData` | ~$0.01 per 1,000 metrics requested — under $1/month |
| Container Insights | not enabled, deliberately |

## Future slices

Not in scope here; recorded so the sequencing is explicit.

2. **Application and business metrics** — SerpAPI credit burn, stub-fallback
   rate, review PASS/FAIL rate, replan-loop depth, `plan-trip` latency. Needs a
   new `app/services/metrics.py` and call sites across the agents, pushing via
   Prometheus `remote_write`. The highest-value slice, because none of this is
   observable anywhere today: SerpAPI credit exhaustion currently surfaces only
   as flights quietly reverting to stubs.
3. **AWS cost tracking** — billing metrics, `us-east-1` only.
4. **LLM cost and tokens** — heavy overlap with Langfuse; may be dropped
   entirely rather than duplicated.

## Sources

- [Grafana pricing](https://grafana.com/pricing/) — free tier limits
- [Configure AWS authentication, Grafana Cloud](https://grafana.com/docs/grafana-cloud/connect-externally-hosted/data-sources/aws-cloudwatch/aws-authentication/) — assume-role and external ID
- [Amazon ECS CloudWatch metrics](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cloudwatch-container-insights.html) — Container Insights vs built-in `AWS/ECS` metrics
- [Amazon ECS cluster utilization metrics](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cluster_utilization.html) — not available for Fargate
