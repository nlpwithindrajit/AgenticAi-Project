# ECS task definition templates

These are passed verbatim to `aws ecs register-task-definition --cli-input-json`
after placeholder substitution, so they may contain **only** keys that command
accepts. There is no comment syntax in JSON and no `_comment` escape hatch — the
CLI rejects unknown parameters — which is why this file exists.

## Placeholders

| placeholder | substituted with | notes |
|---|---|---|
| `__ACCOUNT__` | AWS account ID | |
| `__REGION__` | AWS region | |
| `__IMAGE__` | full ECR image reference | tagged with the git SHA, never `latest`, so a rollback has something to point at |
| `__BASE_URL__` | the load balancer URL, **bare** | `http://name.region.elb.amazonaws.com` — no quotes, no brackets |

`__BASE_URL__` is deliberately a bare URL. `ALLOWED_ORIGINS` has to reach the
container as the string `["http://..."]`, and the quoting for that lives in the
template (`"[\"__BASE_URL__\"]"`). Substituting an already-quoted array into a
JSON string field produces unescaped quotes and invalid JSON — which fails at
`register-task-definition` with a parse error that does not name the field.

## Secrets

No key appears in these files. `OPENAI_API_KEY`, `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` are referenced from SSM Parameter Store under
`/travel-planner/` and injected by the ECS agent at task start.

This is not decoration. Task definitions are readable by anyone holding
`ecs:DescribeTaskDefinition`, and every revision is retained permanently with no
way to delete the value — a key pasted into one is effectively published, and
rotating it is the only remedy.

The execution role needs `ssm:GetParameters` and `kms:Decrypt` for this to work;
`provision-ecs.sh` attaches that inline policy. Without it, tasks fail at start
with `ResourceInitializationError` and never reach your application logs.

## Platform

`runtimePlatform.cpuArchitecture` is `X86_64`. Images built on an Apple Silicon
Mac without `--platform linux/amd64` will register fine and then die at start
with `exec format error`, visible only in CloudWatch.
