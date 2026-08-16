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
    ["test.yml", "deploy.yml", "provision.yml", "teardown.yml", "sync-secrets.yml"],
)
def test_workflow_is_valid_yaml(name: str) -> None:
    parsed = yaml.safe_load((WORKFLOWS / name).read_text())
    assert parsed["jobs"], f"{name} defines no jobs"


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
