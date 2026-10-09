"""The scheduled research run: opt-in, least privilege, on time, and loud when it fails."""

from __future__ import annotations

import json

import pytest
from conftest import ACCOUNT, REGION, deploy
from traider.config import Config

TASK = "aws:ecs/taskDefinition:TaskDefinition"
ROLE = "aws:iam/role:Role"
ROLE_POLICY = "aws:iam/rolePolicy:RolePolicy"
SECRET = "aws:secretsmanager/secret:Secret"
TABLE = "aws:dynamodb/table:Table"
TOPIC = "aws:sns/topic:Topic"
BUCKET = "aws:s3/bucket:Bucket"
SCHEDULE = "aws:scheduler/schedule:Schedule"
RULE = "aws:cloudwatch/eventRule:EventRule"
CLUSTER = "aws:ecs/cluster:Cluster"


@pytest.fixture(scope="module")
def jobs():
    return deploy({"research": True, "researchJobs": True, "alertEmail": "ops@example.test"})


def container(deployment) -> dict:
    (definition,) = json.loads(deployment.one(TASK, "research").inputs["containerDefinitions"])
    return definition


def environment(deployment) -> dict[str, str]:
    return {item["name"]: item["value"] for item in container(deployment)["environment"]}


def statements(deployment, name) -> dict[str, dict]:
    return {s["Sid"]: s for s in deployment.policy(name)}


# --- opt-in ---------------------------------------------------------------------------


def test_research_jobs_are_off_by_default(paper):
    assert paper.of(BUCKET) == []
    assert paper.of(SCHEDULE) == []
    assert [t for t in paper.of(TASK) if t.name == "research"] == []
    assert {s.name for s in paper.of(SECRET)} == {"schwab-app", "schwab-token"}
    assert "finnhubSecretArn" not in paper.outputs


def test_research_on_alone_creates_no_jobs():
    assert deploy({"research": True}).of(SCHEDULE) == []


def test_research_jobs_without_research_are_refused():
    with pytest.raises(Exception, match="researchJobs needs traider:research"):
        deploy({"researchJobs": True})


# --- the trail bucket --------------------------------------------------------------------


def test_the_trail_bucket_is_private_encrypted_tls_only_and_expiring(jobs):
    bucket = jobs.one(BUCKET, "research-trail")
    assert bucket.inputs["bucket"] == f"traider-dev-research-trail-{ACCOUNT}"
    block = jobs.one("aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock").inputs
    assert block["bucket"] == bucket.id
    assert all(
        block[k]
        for k in (
            "blockPublicAcls",
            "blockPublicPolicy",
            "ignorePublicAcls",
            "restrictPublicBuckets",
        )
    )
    sse = jobs.one(
        "aws:s3/bucketServerSideEncryptionConfiguration:BucketServerSideEncryptionConfiguration"
    ).inputs
    (rule,) = sse["rules"]
    assert rule["applyServerSideEncryptionByDefault"]["sseAlgorithm"] == "AES256"
    lifecycle = jobs.one("aws:s3/bucketLifecycleConfiguration:BucketLifecycleConfiguration").inputs
    (expire,) = lifecycle["rules"]
    assert (expire["status"], expire["expiration"]["days"]) == ("Enabled", 400)
    policy = json.loads(jobs.one("aws:s3/bucketPolicy:BucketPolicy").inputs["policy"])
    (deny,) = policy["Statement"]
    assert (deny["Effect"], deny["Principal"], deny["Action"]) == ("Deny", "*", "s3:*")
    assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert deny["Resource"] == [bucket.arn, f"{bucket.arn}/*"]


def test_a_live_stack_keeps_its_trail_on_destroy(jobs):
    live = deploy(
        {
            "research": True,
            "researchJobs": True,
            "tradingMode": "live",
            "accountLast4": "5678",
            "alertEmail": "ops@example.test",
        },
        stack="prod",
    )
    assert live.one(BUCKET).inputs["forceDestroy"] is False
    assert jobs.one(BUCKET).inputs["forceDestroy"] is True


# --- the task ---------------------------------------------------------------------------


def test_the_task_is_the_bots_image_running_the_premarket_command(jobs):
    task = jobs.one(TASK, "research").inputs
    assert task["family"] == "traider-dev-research"
    assert (task["cpu"], task["memory"]) == ("512", "1024")
    assert task["requiresCompatibilities"] == ["FARGATE"]
    assert task["executionRoleArn"] == jobs.one(ROLE, "bot-execution").arn
    assert task["taskRoleArn"] == jobs.one(ROLE, "research-task").arn
    definition = container(jobs)
    bot = json.loads(jobs.one(TASK, "bot").inputs["containerDefinitions"])[0]
    assert definition["image"] == bot["image"]
    assert definition["command"] == ["research", "run", "--kind", "premarket"]
    assert "secrets" not in definition  # not even the sign-in link
    logs = definition["logConfiguration"]["options"]
    assert logs["awslogs-group"] == "/traider/traider-dev/research"


def test_the_task_is_told_where_everything_is_and_nothing_secret(jobs):
    env = environment(jobs)
    assert env["TRAIDER_RESEARCH_TABLE"] == jobs.one(TABLE, "research").inputs["name"]
    assert env["TRAIDER_SETTINGS_TABLE"] == jobs.one(TABLE, "settings").inputs["name"]
    assert env["TRAIDER_RESEARCH_BUCKET"] == f"traider-dev-research-trail-{ACCOUNT}"
    assert env["TRAIDER_FINNHUB_SECRET_ID"] == jobs.one(SECRET, "finnhub").arn
    assert env["TRAIDER_SCHWAB_TOKEN_SECRET_ID"] == jobs.one(SECRET, "schwab-token").arn
    assert env["TRAIDER_ALERT_TOPIC_ARN"] == jobs.one(TOPIC).arn
    assert "TRAIDER_TRADING_MODE" not in env  # research never trades
    assert "TRAIDER_FINNHUB_API_KEY" not in env
    config = Config.from_env(env)
    assert (config.trading_mode, config.finnhub_api_key) == ("paper", None)


def test_a_live_stacks_research_task_still_starts():
    live = deploy(
        {
            "research": True,
            "researchJobs": True,
            "tradingMode": "live",
            "accountLast4": "5678",
            "alertEmail": "ops@example.test",
        },
        stack="prod",
    )
    assert Config.from_env(environment(live)).research_table == "traider-prod-research"


def test_the_finnhub_secret_is_created_without_a_value(jobs):
    assert jobs.one(SECRET, "finnhub")
    assert jobs.of("aws:secretsmanager/secretVersion:SecretVersion") == []
    text = json.dumps(jobs.outputs) + jobs.one(TASK, "research").inputs["containerDefinitions"]
    assert "api_key" not in text
    assert "finnhubSecretArn" not in jobs.secret_outputs  # an ARN, not a secret


# --- least privilege -----------------------------------------------------------------------


def test_the_research_role_names_exactly_the_resources_it_uses(jobs):
    granted = {sid: s["Resource"] for sid, s in statements(jobs, "research-task").items()}
    research = jobs.one(TABLE, "research").arn
    assert granted == {
        "ReadSecrets": [
            jobs.one(SECRET, "schwab-app").arn,
            jobs.one(SECRET, "schwab-token").arn,
            jobs.one(SECRET, "finnhub").arn,
        ],
        "SaveRotatedRefreshToken": jobs.one(SECRET, "schwab-token").arn,
        "Research": research,
        "ResearchRunsByDay": f"{research}/index/gsi1",
        "ReadSettings": jobs.one(TABLE, "settings").arn,
        "Trail": f"{jobs.one(BUCKET).arn}/*",
        "Alerts": jobs.one(TOPIC).arn,
        "BedrockMantle": "*",
    }


def test_the_research_role_has_exactly_these_actions(jobs):
    found = {sid: s["Action"] for sid, s in statements(jobs, "research-task").items()}
    assert found == {
        "ReadSecrets": "secretsmanager:GetSecretValue",
        "SaveRotatedRefreshToken": "secretsmanager:PutSecretValue",
        "Research": [
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:Query",
        ],
        "ResearchRunsByDay": "dynamodb:Query",
        "ReadSettings": ["dynamodb:Query", "dynamodb:GetItem"],
        "Trail": "s3:PutObject",
        "Alerts": "sns:Publish",
        "BedrockMantle": [
            "bedrock-mantle:CreateInference",
            "bedrock-mantle:GetProject",
            "bedrock-mantle:ListProjects",
        ],
    }


def test_only_bedrock_uses_a_wildcard_resource(jobs):
    for policy in jobs.of(ROLE_POLICY):
        for statement in json.loads(policy.inputs["policy"])["Statement"]:
            if statement["Sid"] == "BedrockMantle":
                continue
            values = (
                statement["Resource"]
                if isinstance(statement["Resource"], list)
                else [statement["Resource"]]
            )
            actions = (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
            for value in values + actions:
                assert "*" not in value.replace(":*", "").replace("/*", ""), value


def test_the_bot_still_only_reads_research(jobs):
    (statement,) = [s for s in jobs.policy("bot-task") if s["Sid"] == "Research"]
    assert set(statement["Action"]) == {"dynamodb:Query", "dynamodb:GetItem"}


def test_roles_are_assumed_only_by_their_service(jobs):
    def principal(name):
        trust = json.loads(jobs.one(ROLE, name).inputs["assumeRolePolicy"])
        return trust["Statement"][0]["Principal"]["Service"]

    assert principal("research-task") == "ecs-tasks.amazonaws.com"
    assert principal("research-scheduler") == "scheduler.amazonaws.com"


def test_the_scheduler_may_start_only_this_task_and_pass_only_its_roles(jobs):
    found = statements(jobs, "research-scheduler")
    run = found["StartResearchTask"]
    assert run["Action"] == "ecs:RunTask"
    assert run["Resource"] == (
        f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/traider-dev-research:*"
    )
    assert run["Condition"] == {"ArnEquals": {"ecs:cluster": jobs.one(CLUSTER, "research").arn}}
    passing = found["PassResearchRoles"]
    assert passing["Action"] == "iam:PassRole"
    assert passing["Resource"] == [
        jobs.one(ROLE, "research-task").arn,
        jobs.one(ROLE, "bot-execution").arn,
    ]


# --- the schedule ---------------------------------------------------------------------------


def test_it_runs_weekdays_at_8_new_york_time_on_time_and_never_retried(jobs):
    schedule = jobs.one(SCHEDULE).inputs
    assert schedule["scheduleExpression"] == "cron(0 8 ? * MON-FRI *)"
    assert schedule["scheduleExpressionTimezone"] == "America/New_York"
    assert schedule["flexibleTimeWindow"] == {"mode": "OFF"}
    target = schedule["target"]
    assert target["retryPolicy"]["maximumRetryAttempts"] == 0
    assert target["arn"] == jobs.one(CLUSTER, "research").arn
    assert target["roleArn"] == jobs.one(ROLE, "research-scheduler").arn
    ecs = target["ecsParameters"]
    assert ecs["taskDefinitionArn"] == jobs.one(TASK, "research").arn
    assert (ecs["launchType"], ecs["taskCount"]) == ("FARGATE", 1)


def test_it_runs_on_the_bots_network(jobs):
    service = jobs.one("aws:ecs/service:Service").inputs["networkConfiguration"]
    network = jobs.one(SCHEDULE).inputs["target"]["ecsParameters"]["networkConfiguration"]
    assert network["subnets"] == service["subnets"]
    assert network["securityGroups"] == service["securityGroups"]
    assert network["assignPublicIp"] is True


def test_it_has_its_own_cluster_so_the_bots_crash_alarm_stays_quiet(jobs):
    research = jobs.one(CLUSTER, "research").inputs["name"]
    assert research == "traider-dev-research"
    bot_rule = json.loads(jobs.one(RULE, "bot-stopped").inputs["eventPattern"])
    assert bot_rule["detail"]["clusterArn"] == [jobs.one(CLUSTER, "bot").arn]


# --- failures are loud --------------------------------------------------------------------


def test_a_research_task_that_fails_raises_an_alert(jobs):
    rule = jobs.one(RULE, "research-failed")
    detail = json.loads(rule.inputs["eventPattern"])["detail"]
    assert detail["clusterArn"] == [jobs.one(CLUSTER, "research").arn]
    assert detail["group"] == ["family:traider-dev-research"]
    assert detail["lastStatus"] == ["STOPPED"]
    assert detail["$or"] == [
        {"stopCode": ["TaskFailedToStart"]},
        {"containers": {"exitCode": [{"anything-but": 0}]}},
    ]
    target = jobs.one("aws:cloudwatch/eventTarget:EventTarget", "research-failed").inputs
    assert target["arn"] == jobs.one(TOPIC).arn
    assert "[traider] The research run" in target["inputTransformer"]["inputTemplate"]


def test_both_alarms_may_publish_to_the_topic(jobs):
    policy = json.loads(jobs.one("aws:sns/topicPolicy:TopicPolicy").inputs["policy"])
    sources = {s["Sid"]: s["Condition"]["ArnEquals"]["aws:SourceArn"] for s in policy["Statement"]}
    assert sources == {
        "TaskStoppedAlarm": jobs.one(RULE, "bot-stopped").arn,
        "ResearchFailedAlarm": jobs.one(RULE, "research-failed").arn,
    }


# --- outputs and local use ------------------------------------------------------------------


def local_env(deployment) -> dict[str, str]:
    import shlex

    pairs = [shlex.split(line) for line in deployment.outputs["localEnv"].splitlines()]
    return dict(pair[0].split("=", 1) for pair in pairs)


def test_outputs_say_where_the_research_run_lives(jobs):
    out = jobs.outputs
    assert out["researchBucket"] == f"traider-dev-research-trail-{ACCOUNT}"
    assert out["finnhubSecretArn"] == jobs.one(SECRET, "finnhub").arn
    assert out["researchCluster"] == "traider-dev-research"
    assert out["researchLogGroup"] == "/traider/traider-dev/research"


def test_local_env_lets_you_dry_run_research_and_still_cannot_trade(jobs):
    lines = local_env(jobs)
    assert lines["TRAIDER_FINNHUB_SECRET_ID"] == jobs.one(SECRET, "finnhub").arn
    assert "TRAIDER_RESEARCH_BUCKET" not in lines  # a local run keeps its trail locally
    assert "TRAIDER_TRADING_MODE" not in lines
    assert "TRAIDER_CONTROL_PARAM" not in lines
