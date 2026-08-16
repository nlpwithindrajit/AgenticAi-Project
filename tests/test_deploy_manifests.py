"""Checks on the deployment manifests.

These files are only exercised on a real deploy, where a mistake costs a failed
release and a CloudWatch dig. They are cheap to check here instead.

The bug that motivated this: `ALLOWED_ORIGINS` used to be substituted as an
already-quoted JSON array into a JSON *string* field, which produced unescaped
quotes and invalid JSON. `register-task-definition` rejected it with a parse
error that did not name the field. Nothing caught it until a deploy ran.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ECS = ROOT / "deploy" / "ecs"
WORKFLOWS = ROOT / ".github" / "workflows"

ACCOUNT = "111122223333"
REGION = "us-east-1"
BASE_URL = "http://travel-planner-alb-123456.us-east-1.elb.amazonaws.com"

# The keys `aws ecs register-task-definition` accepts at the top level. Anything
# else — including a `_comment` field — is rejected as an unknown parameter, so
# there is no way to leave a note inside these files. See deploy/ecs/README.md.
ALLOWED_TOP_LEVEL = {
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "volumes",
    "placementConstraints",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "tags",
    "pidMode",
    "ipcMode",
    "proxyConfiguration",
    "inferenceAccelerators",
    "ephemeralStorage",
    "runtimePlatform",
    "enableFaultInjection",
}

TASK_DEFS = sorted(ECS.glob("task-def-*.json"))


def render(path: Path) -> str:
    """Substitute placeholders exactly as the deploy scripts and workflow do."""
    image = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/travel-planner-x:abc1234"
    return (
        path.read_text()
        .replace("__ACCOUNT__", ACCOUNT)
        .replace("__REGION__", REGION)
        .replace("__IMAGE__", image)
        .replace("__BASE_URL__", BASE_URL)
    )


def test_there_are_task_definitions() -> None:
    """Guard the glob: an empty parametrise would pass silently."""
    assert {p.name for p in TASK_DEFS} == {
        "task-def-api.json",
        "task-def-ui.json",
    }


@pytest.mark.parametrize("path", TASK_DEFS, ids=lambda p: p.name)
def test_renders_to_valid_json(path: Path) -> None:
    rendered = render(path)
    assert not re.findall(r"__[A-Z_]+__", rendered), "placeholder left unsubstituted"
    json.loads(rendered)  # the actual assertion


@pytest.mark.parametrize("path", TASK_DEFS, ids=lambda p: p.name)
def test_only_uses_keys_the_cli_accepts(path: Path) -> None:
    unknown = set(json.loads(render(path))) - ALLOWED_TOP_LEVEL
    assert not unknown, f"register-task-definition would reject: {sorted(unknown)}"


@pytest.mark.parametrize("path", TASK_DEFS, ids=lambda p: p.name)
def test_targets_the_architecture_fargate_runs(path: Path) -> None:
    """An arm64 image built on a Mac dies with `exec format error` at start."""
    d = json.loads(render(path))
    assert d["runtimePlatform"]["cpuArchitecture"] == "X86_64"
    assert d["requiresCompatibilities"] == ["FARGATE"]
    assert d["networkMode"] == "awsvpc"  # required by Fargate


@pytest.mark.parametrize("path", TASK_DEFS, ids=lambda p: p.name)
def test_no_literal_secret_is_ever_written_into_a_task_definition(path: Path) -> None:
    """Task definition revisions are permanent and widely readable.

    A key pasted into one cannot be deleted, so it has to be treated as
    published. Secrets belong in SSM, referenced by ARN.
    """
    blob = render(path)
    for marker in ("sk-proj-", "sk-ant-", "sk-lf-", "pk-lf-", "AKIA"):
        assert marker not in blob, f"{marker}... appears in {path.name}"

    for container in json.loads(blob)["containerDefinitions"]:
        for secret in container.get("secrets", []):
            assert secret["valueFrom"].startswith("arn:aws:ssm:"), secret


def test_allowed_origins_is_parseable_by_pydantic_settings() -> None:
    """The API reads ALLOWED_ORIGINS as a list, so it must arrive as JSON."""
    api = json.loads(render(ECS / "task-def-api.json"))
    env = {e["name"]: e["value"] for e in api["containerDefinitions"][0]["environment"]}
    assert json.loads(env["ALLOWED_ORIGINS"]) == [BASE_URL]


def test_api_health_check_matches_the_target_group_path() -> None:
    """The ALB probes /api/health; a mismatch takes the service down.

    provision-ecs.sh registers the target group with that path, and the API
    only answers it because main.py mirrors the router under /api.
    """
    assert "/api/health" in (ROOT / "deploy" / "provision-ecs.sh").read_text()


@pytest.mark.parametrize(
    "name",
    [
        "test.yml",
        "deploy.yml",
        "provision.yml",
        "teardown.yml",
        "sync-secrets.yml",
        "monitoring-snapshot.yml",
        "provision-grafana.yml",
    ],
)
def test_workflow_is_valid_yaml(name: str) -> None:
    parsed = yaml.safe_load((WORKFLOWS / name).read_text())
    assert parsed["jobs"], f"{name} defines no jobs"


GRAFANA = ROOT / "deploy" / "grafana"

# Verbs that only read. `Start`/`Stop` are here for logs:StartQuery and
# logs:StopQuery, which begin and end a Logs Insights read.
READ_ONLY_VERBS = ("Get", "List", "Describe", "Start", "Stop")


def test_the_grafana_role_can_only_read() -> None:
    """A monitoring role that can write is easy to create and hard to notice.

    Nothing about a dashboard changes if the role behind it is over-permissive,
    so the policy is pinned here rather than trusted to review.
    """
    policy = json.loads((GRAFANA / "readonly-policy.json").read_text())

    actions = [
        action
        for statement in policy["Statement"]
        for action in statement["Action"]
    ]
    assert actions, "empty policy would pass every assertion below"

    for action in actions:
        assert action != "*", "wildcard action"
        service, _, verb = action.partition(":")
        assert verb and verb != "*", f"service-wide wildcard: {action}"
        assert verb.startswith(READ_ONLY_VERBS), f"not a read-only action: {action}"

    for statement in policy["Statement"]:
        assert statement["Effect"] == "Allow"


def test_the_grafana_trust_policy_requires_an_external_id() -> None:
    """Without the condition the role is assumable by any Grafana Cloud tenant.

    That misconfiguration is invisible: the data source connects, the panels
    render, and nothing anywhere reports that the door is open.
    """
    trust = json.loads(
        (GRAFANA / "trust-policy.json")
        .read_text()
        .replace("__GRAFANA_ACCOUNT_ID__", "008923505280")
        .replace("__EXTERNAL_ID__", "test-external-id")
    )
    statement = trust["Statement"][0]

    assert statement["Action"] == "sts:AssumeRole"
    condition = statement["Condition"]["StringEquals"]["sts:ExternalId"]
    assert condition == "test-external-id"


def test_the_grafana_policies_render_without_leftover_placeholders() -> None:
    rendered = (
        (GRAFANA / "trust-policy.json")
        .read_text()
        .replace("__GRAFANA_ACCOUNT_ID__", "008923505280")
        .replace("__EXTERNAL_ID__", "abc123")
    )
    assert not re.findall(r"__[A-Z_]+__", rendered)
    # The read-only policy is copied verbatim, so it must need no substitution.
    assert not re.findall(
        r"__[A-Z_]+__", (GRAFANA / "readonly-policy.json").read_text()
    )


def test_cloudwatch_dimensions_are_the_arn_suffix_not_the_whole_arn() -> None:
    """This shipped broken once, and nothing looked wrong.

    A CloudWatch dimension of the wrong shape is still a valid query — it just
    matches no metric, so `HealthyHostCount` came back empty and read exactly
    like a service with no healthy targets. `${arn#*:}` strips only "arn:";
    the dimension needs everything after the LAST colon.
    """
    arn = (
        "arn:aws:elasticloadbalancing:us-east-1:024899754281:"
        "targetgroup/travel-planner-api-tg/0123456789abcdef"
    )
    extracted = subprocess.run(
        ["bash", "-c", f'arn="{arn}"; printf %s "${{arn##*:}}"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert extracted == "targetgroup/travel-planner-api-tg/0123456789abcdef"

    script = (ROOT / "deploy" / "monitoring-snapshot.sh").read_text()
    assert "${arn##*:}" in script, "target group dimension must strip to the suffix"
    assert "${arn#*:}" not in script, "single # leaves the whole ARN minus 'arn:'"


def test_the_monitoring_snapshot_cannot_change_anything() -> None:
    """It runs with the deploy credentials, which can delete the cluster.

    Its safety comes only from what it calls, so that is asserted rather than
    trusted — a snapshot that mutates would be indistinguishable from one that
    does not until the day it removes something.
    """
    script = (ROOT / "deploy" / "monitoring-snapshot.sh").read_text()

    forbidden = (
        "delete-",
        "create-",
        "update-",
        "put-",
        "register-",
        "deregister-",
        "modify-",
        "terminate-",
        "run-task",
        "stop-",
    )
    # `aws elbv2 describe-...`, `aws cloudwatch get-metric-data` are the only
    # shapes expected; anything else is a mutation waiting to happen.
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped.startswith("aws "):
            continue
        assert not any(verb in stripped for verb in forbidden), (
            f"mutating AWS call in monitoring-snapshot.sh: {stripped}"
        )


def test_flight_provider_key_reaches_the_api() -> None:
    """Without this the deployed API silently serves STUB flights.

    It fails open by design — a missing key is a note in TripPlan.errors, not
    a crash — which is exactly why nothing else would catch the omission.
    """
    api = json.loads(render(ECS / "task-def-api.json"))
    secrets = {
        s["name"]: s["valueFrom"]
        for container in api["containerDefinitions"]
        for s in container.get("secrets", [])
    }
    assert "SERPAPI_API_KEY" in secrets
    assert secrets["SERPAPI_API_KEY"].endswith(
        "parameter/travel-planner/SERPAPI_API_KEY"
    )


def test_every_referenced_secret_is_actually_synced() -> None:
    """A task definition can reference an SSM parameter nobody ever writes.

    ECS then refuses to start the task with a ResourceNotFound the service
    events bury, so the two lists are pinned together here instead.
    """
    referenced = {
        s["name"]
        for path in TASK_DEFS
        for container in json.loads(render(path))["containerDefinitions"]
        for s in container.get("secrets", [])
    }
    sync = (WORKFLOWS / "sync-secrets.yml").read_text()
    provision = (ROOT / "deploy" / "provision-ecs.sh").read_text()

    missing_sync = {n for n in referenced if f"put {n} " not in sync}
    assert not missing_sync, f"referenced but never synced: {missing_sync}"

    missing_provision = {n for n in referenced if f"put_secret {n} " not in provision}
    assert not missing_provision, (
        f"referenced but not seeded by provision-ecs.sh: {missing_provision}"
    )


def test_deploy_cannot_run_without_the_test_suite() -> None:
    """The whole point of the gate: no path to production skips the tests."""
    deploy = yaml.safe_load((WORKFLOWS / "deploy.yml").read_text())
    assert deploy["jobs"]["test"]["uses"] == "./.github/workflows/test.yml"
    assert "test" in deploy["jobs"]["deploy"]["needs"]

    # ...which requires test.yml to actually be callable.
    test_wf = yaml.safe_load((WORKFLOWS / "test.yml").read_text())
    triggers = test_wf[True] if True in test_wf else test_wf["on"]
    assert "workflow_call" in triggers


def test_nothing_deploys_automatically_on_a_push_to_main() -> None:
    """Fargate bills continuously — shipping stays an explicit decision."""
    deploy = yaml.safe_load((WORKFLOWS / "deploy.yml").read_text())
    triggers = deploy[True] if True in deploy else deploy["on"]
    assert set(triggers["push"]) == {"tags"}, "deploy must not trigger on branches"


@pytest.mark.parametrize("name", ["deploy.yml", "provision.yml", "teardown.yml"])
def test_billable_workflows_cannot_race_each_other(name: str) -> None:
    """One concurrency group, so a deploy and a teardown cannot interleave."""
    wf = yaml.safe_load((WORKFLOWS / name).read_text())
    assert wf["concurrency"]["group"] == "deploy-production"
    assert wf["concurrency"]["cancel-in-progress"] is False
