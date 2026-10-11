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
QUEUE = "aws:sqs/queue:Queue"
ALARM = "aws:cloudwatch/metricAlarm:MetricAlarm"
TOPIC_POLICY = "aws:sns/topicPolicy:TopicPolicy"
LOG_GROUP = "aws:cloudwatch/logGroup:LogGroup"


KINDS = ("premarket", "scorecard", "intraday")
TOGGLES = {
    "premarket": "researchScheduleEnabled",
    "scorecard": "researchScorecardEnabled",
    "intraday": "researchIntradayEnabled",
}
# Each kind has its own task definition (and family); the pre-market one keeps its name.
TASK_NAMES = {
    "premarket": "research",
    "scorecard": "research-scorecard",
    "intraday": "research-intraday",
}
FAMILIES = {
    "premarket": "traider-dev-research",
    "scorecard": "traider-dev-research-scorecard",
    "intraday": "traider-dev-research-intraday",
}


@pytest.fixture(scope="module")
def jobs():
    return deploy({"research": True, "researchJobs": True, "alertEmail": "ops@example.test"})


def task(deployment, kind="premarket"):
    return deployment.one(TASK, TASK_NAMES[kind])


def container(deployment, kind="premarket") -> dict:
    (definition,) = json.loads(task(deployment, kind).inputs["containerDefinitions"])
    return definition


def environment(deployment, kind="premarket") -> dict[str, str]:
    return {item["name"]: item["value"] for item in container(deployment, kind)["environment"]}


def schedule(deployment, kind="premarket"):
    return deployment.one(SCHEDULE, f"research-{kind}")


def statements(deployment, name) -> dict[str, dict]:
    return {s["Sid"]: s for s in deployment.policy(name)}


# --- opt-in ---------------------------------------------------------------------------


def test_research_jobs_are_off_by_default(paper):
    assert paper.of(BUCKET) == []
    assert paper.of(SCHEDULE) == []
    assert [t for t in paper.of(TASK) if t.name.startswith("research")] == []
    assert {s.name for s in paper.of(SECRET)} == {"schwab-app", "schwab-token"}
    assert "finnhubSecretArn" not in paper.outputs


def test_research_on_alone_creates_no_jobs():
    assert deploy({"research": True}).of(SCHEDULE) == []


@pytest.mark.parametrize("config", [{}, {"research": True}], ids=["default", "research-only"])
def test_without_the_jobs_there_is_no_dead_letter_queue_or_new_alarm(config):
    deployment = deploy({**config, "alertEmail": "ops@example.test"})
    assert deployment.of(QUEUE) == []
    assert deployment.of(ALARM) == []
    policy = json.loads(deployment.one(TOPIC_POLICY).inputs["policy"])
    assert [s["Sid"] for s in policy["Statement"]] == ["TaskStoppedAlarm"]


def test_research_jobs_without_research_are_refused():
    with pytest.raises(Exception, match="researchJobs needs traider:research"):
        deploy({"researchJobs": True})


def test_every_schedule_starts_disabled(jobs):
    assert {s.name: s.inputs["state"] for s in jobs.of(SCHEDULE)} == {
        f"research-{kind}": "DISABLED" for kind in KINDS
    }


@pytest.mark.parametrize("kind", KINDS)
def test_each_schedule_runs_only_once_you_enable_it_and_only_that_one(kind):
    enabled = deploy({"research": True, "researchJobs": True, TOGGLES[kind]: True})
    states = {s.name: s.inputs["state"] for s in enabled.of(SCHEDULE)}
    assert states == {
        f"research-{other}": "ENABLED" if other == kind else "DISABLED" for other in KINDS
    }
    disabled = deploy({"research": True, "researchJobs": True, TOGGLES[kind]: False})
    assert schedule(disabled, kind).inputs["state"] == "DISABLED"


@pytest.mark.parametrize("toggle", TOGGLES.values())
@pytest.mark.parametrize("config", [{}, {"research": True}], ids=["default", "research-only"])
def test_enabling_a_schedule_without_the_jobs_is_refused(config, toggle):
    with pytest.raises(Exception, match=f"{toggle} needs traider:researchJobs"):
        deploy({**config, toggle: True})


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


@pytest.mark.parametrize(
    "stack",
    ["Dev", "dev_1", "a-stack-name-long-enough-to-overflow-63"],
    ids=["uppercase", "underscore", "too-long"],
)
def test_a_stack_name_s3_would_refuse_fails_at_load(stack):
    with pytest.raises(Exception, match="research trail bucket would be named"):
        deploy({"research": True, "researchJobs": True}, stack=stack)


def test_the_longest_allowed_stack_name_still_deploys():
    stack = "s" * (35 - len("traider-"))  # the bucket name is then exactly 63 characters
    deployment = deploy({"research": True, "researchJobs": True}, stack=stack)
    assert len(deployment.one(BUCKET).inputs["bucket"]) == 63


def test_a_bad_stack_name_is_fine_without_the_jobs():
    assert deploy({"research": True}, stack="Dev").of(BUCKET) == []


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


def test_each_kind_has_its_own_task_differing_only_in_family_and_command(jobs):
    assert {t.name for t in jobs.of(TASK) if t.name.startswith("research")} == set(
        TASK_NAMES.values()
    )
    premarket = task(jobs).inputs
    for kind in KINDS:
        found = task(jobs, kind).inputs
        assert found["family"] == FAMILIES[kind]
        definition = container(jobs, kind)
        assert definition["name"] == "research"
        assert definition["command"] == ["research", "run", "--kind", kind]
        # Same image, roles, size, network mode, environment and log group as pre-market.
        assert {k: v for k, v in definition.items() if k != "command"} == {
            k: v for k, v in container(jobs).items() if k != "command"
        }
        same = {k: v for k, v in found.items() if k not in ("family", "containerDefinitions")}
        assert same == {
            k: v for k, v in premarket.items() if k not in ("family", "containerDefinitions")
        }


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
    assert env["AWS_REGION"] == REGION  # Bedrock needs it; not left to Fargate
    # The bot's state, to read: the namespace is the stack's trading mode, given apart.
    assert env["TRAIDER_STATE_TABLE"] == jobs.one(TABLE, "state").inputs["name"]
    assert env["TRAIDER_STATE_NAMESPACE"] == "paper"
    config = Config.from_env(env)
    assert (config.trading_mode, config.finnhub_api_key) == ("paper", None)
    assert (config.state_table, config.state_namespace) == (
        jobs.one(TABLE, "state").inputs["name"],
        "paper",
    )


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
    config = Config.from_env(environment(live))
    assert config.research_table == "traider-prod-research"
    assert (config.trading_mode, config.state_namespace) == ("paper", "live")


ACCOUNT_IDS = ("TRAIDER_SCHWAB_ACCOUNT_HASH", "TRAIDER_SCHWAB_ACCOUNT_LAST4")


def test_the_research_task_gets_no_account_identifiers():
    live = deploy(
        {
            "research": True,
            "researchJobs": True,
            "tradingMode": "live",
            "accountLast4": "9753",
            "accountHash": "HASH0123456789",
            "alertEmail": "ops@example.test",
        },
        stack="prod",
    )
    env = environment(live)
    for name in ACCOUNT_IDS:
        assert name not in env, name
    assert "9753" not in json.dumps(env) and "HASH0123456789" not in json.dumps(env)
    config = Config.from_env(env)
    assert (config.account_hash, config.account_last4) == (None, None)
    # The bot still gets them.
    bot_env = {
        item["name"]: item["value"]
        for item in json.loads(live.one(TASK, "bot").inputs["containerDefinitions"])[0][
            "environment"
        ]
    }
    assert bot_env["TRAIDER_SCHWAB_ACCOUNT_LAST4"] == "9753"
    assert bot_env["TRAIDER_SCHWAB_ACCOUNT_HASH"] == "HASH0123456789"


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
        "ReadBotState": jobs.one(TABLE, "state").arn,
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
            "dynamodb:BatchGetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:Query",
        ],
        "ResearchRunsByDay": "dynamodb:Query",
        "ReadSettings": ["dynamodb:Query", "dynamodb:GetItem"],
        "ReadBotState": "dynamodb:Query",
        "Trail": "s3:PutObject",
        "Alerts": "sns:Publish",
        "BedrockMantle": [
            "bedrock-mantle:CreateInference",
            "bedrock-mantle:GetProject",
            "bedrock-mantle:ListProjects",
        ],
    }


def _listed(value) -> list[str]:
    return value if isinstance(value, list) else [value]


def test_wildcards_appear_only_where_they_must(jobs):
    """A resource with a "*" is allowed only as one of these, and no action has one."""
    allowed = {
        # every revision of each research task family, for the scheduler
        *(f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{f}:*" for f in FAMILIES.values()),
        # the objects in the trail bucket
        f"{jobs.one(BUCKET).arn}/*",
        # a log group's streams
        *(f"{group.arn}:*" for group in jobs.of(LOG_GROUP)),
    }
    for policy in jobs.of(ROLE_POLICY):
        for statement in json.loads(policy.inputs["policy"])["Statement"]:
            actions = _listed(statement["Action"])
            for action in actions:
                assert "*" not in action, (policy.name, statement["Sid"], action)
            for resource in _listed(statement["Resource"]):
                if "*" not in resource:
                    continue
                if resource == "*":
                    assert all(a.startswith("bedrock-mantle:") for a in actions), (
                        policy.name,
                        statement["Sid"],
                    )
                    continue
                assert resource in allowed, (policy.name, statement["Sid"], resource)


def test_the_bot_still_only_reads_research(jobs):
    (statement,) = [s for s in jobs.policy("bot-task") if s["Sid"] == "Research"]
    assert set(statement["Action"]) == {"dynamodb:Query", "dynamodb:GetItem"}


def test_roles_are_assumed_only_by_their_service(jobs):
    def principal(name):
        trust = json.loads(jobs.one(ROLE, name).inputs["assumeRolePolicy"])
        return trust["Statement"][0]["Principal"]["Service"]

    assert principal("research-task") == "ecs-tasks.amazonaws.com"
    assert principal("research-scheduler") == "scheduler.amazonaws.com"


def test_only_this_accounts_scheduler_may_assume_its_role(jobs):
    trust = json.loads(jobs.one(ROLE, "research-scheduler").inputs["assumeRolePolicy"])
    (statement,) = trust["Statement"]
    assert statement["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}


def test_the_scheduler_may_start_only_these_tasks_and_pass_only_their_roles(jobs):
    found = statements(jobs, "research-scheduler")
    run = found["StartResearchTask"]
    assert run["Action"] == "ecs:RunTask"
    assert run["Resource"] == [
        f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{FAMILIES[kind]}:*" for kind in KINDS
    ]
    assert run["Condition"] == {"ArnEquals": {"ecs:cluster": jobs.one(CLUSTER, "research").arn}}
    passing = found["PassResearchRoles"]
    assert passing["Action"] == "iam:PassRole"
    assert passing["Resource"] == [
        jobs.one(ROLE, "research-task").arn,
        jobs.one(ROLE, "bot-execution").arn,
    ]
    assert passing["Condition"] == {
        "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
    }
    dead = found["DeadLetters"]
    assert (dead["Action"], dead["Resource"]) == (
        "sqs:SendMessage",
        jobs.one(QUEUE, "research-schedule-dlq").arn,
    )
    assert set(found) == {"StartResearchTask", "PassResearchRoles", "DeadLetters"}


# --- the schedule ---------------------------------------------------------------------------


def test_it_runs_weekdays_at_8_new_york_time_on_time_and_retries_a_failed_start_briefly(jobs):
    found = schedule(jobs).inputs
    assert found["name"] == "traider-dev-research-premarket"
    assert found["scheduleExpression"] == "cron(0 8 ? * MON-FRI *)"
    assert found["scheduleExpressionTimezone"] == "America/New_York"
    assert found["flexibleTimeWindow"] == {"mode": "OFF"}
    target = found["target"]
    assert target["retryPolicy"] == {"maximumRetryAttempts": 2, "maximumEventAgeInSeconds": 600}
    assert target["arn"] == jobs.one(CLUSTER, "research").arn
    assert target["roleArn"] == jobs.one(ROLE, "research-scheduler").arn
    ecs = target["ecsParameters"]
    assert ecs["taskDefinitionArn"] == jobs.one(TASK, "research").arn
    assert (ecs["launchType"], ecs["taskCount"]) == ("FARGATE", 1)


@pytest.mark.parametrize(
    ("kind", "expression"),
    [("scorecard", "cron(30 16 ? * MON-FRI *)"), ("intraday", "cron(0/30 10-15 ? * MON-FRI *)")],
)
def test_the_other_kinds_run_on_their_own_times_with_their_own_task(jobs, kind, expression):
    found = schedule(jobs, kind).inputs
    assert found["name"] == f"traider-dev-research-{kind}"
    assert found["scheduleExpression"] == expression
    assert found["scheduleExpressionTimezone"] == "America/New_York"
    assert found["flexibleTimeWindow"] == {"mode": "OFF"}
    target = found["target"]
    assert target["ecsParameters"]["taskDefinitionArn"] == task(jobs, kind).arn
    # Everything else is the pre-market schedule's: cluster, role, launch type, network,
    # retries and the dead-letter queue.
    premarket = schedule(jobs).inputs["target"]

    def without_task(t):
        return {**t, "ecsParameters": {**t["ecsParameters"], "taskDefinitionArn": None}}

    assert without_task(target) == without_task(premarket)


def test_each_schedule_starts_a_task_that_runs_exactly_its_own_kind(jobs):
    """No schedule relies on a container override, which Scheduler could drop: the task
    each one starts runs that kind's command and no other."""
    by_arn = {t.arn: t for t in jobs.of(TASK)}
    for kind in KINDS:
        target = schedule(jobs, kind).inputs["target"]
        assert "input" not in target, kind
        started = by_arn[target["ecsParameters"]["taskDefinitionArn"]]
        (definition,) = json.loads(started.inputs["containerDefinitions"])
        assert definition["command"] == ["research", "run", "--kind", kind]
        assert started.inputs["family"] == FAMILIES[kind]


def test_every_schedule_runs_on_the_bots_network(jobs):
    service = jobs.one("aws:ecs/service:Service").inputs["networkConfiguration"]
    for kind in KINDS:
        network = schedule(jobs, kind).inputs["target"]["ecsParameters"]["networkConfiguration"]
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
    assert detail["group"] == [f"family:{FAMILIES[kind]}" for kind in KINDS]
    assert detail["lastStatus"] == ["STOPPED"]
    assert detail["$or"] == [
        {"stopCode": ["TaskFailedToStart"]},
        {"containers": {"exitCode": [{"anything-but": 0}]}},
    ]
    target = jobs.one("aws:cloudwatch/eventTarget:EventTarget", "research-failed").inputs
    assert target["arn"] == jobs.one(TOPIC).arn
    transformer = target["inputTransformer"]
    assert "[traider] The research run" in transformer["inputTemplate"]
    # It says which kind failed: the family names it.
    assert transformer["inputPaths"]["group"] == "$.detail.group"
    assert "<group>" in transformer["inputTemplate"]


def test_a_start_the_scheduler_gives_up_on_lands_in_a_dead_letter_queue(jobs):
    queue = jobs.one(QUEUE, "research-schedule-dlq")
    assert queue.inputs["name"] == "traider-dev-research-schedule-dlq"
    assert queue.inputs["sqsManagedSseEnabled"] is True
    assert queue.inputs["messageRetentionSeconds"] == 14 * 24 * 3600
    assert len(jobs.of(QUEUE)) == 1  # one queue for every schedule
    for kind in KINDS:
        target = schedule(jobs, kind).inputs["target"]
        assert target["deadLetterConfig"] == {"arn": queue.arn}


def test_a_dead_letter_raises_an_alert(jobs):
    alarm = jobs.one(ALARM, "research-schedule-dlq").inputs
    assert (alarm["namespace"], alarm["metricName"]) == (
        "AWS/SQS",
        "ApproximateNumberOfMessagesVisible",
    )
    assert alarm["dimensions"] == {"QueueName": "traider-dev-research-schedule-dlq"}
    assert (alarm["comparisonOperator"], alarm["threshold"]) == ("GreaterThanThreshold", 0)
    assert (alarm["period"], alarm["evaluationPeriods"]) == (300, 1)
    assert alarm["treatMissingData"] == "notBreaching"
    assert alarm["alarmActions"] == [jobs.one(TOPIC).arn]


def test_each_alarm_may_publish_to_the_topic_and_nothing_else_may(jobs):
    policy = json.loads(jobs.one(TOPIC_POLICY).inputs["policy"])
    found = {
        s["Sid"]: (s["Principal"]["Service"], s["Condition"]["ArnEquals"]["aws:SourceArn"])
        for s in policy["Statement"]
    }
    assert found == {
        "TaskStoppedAlarm": ("events.amazonaws.com", jobs.one(RULE, "bot-stopped").arn),
        "ResearchFailedAlarm": ("events.amazonaws.com", jobs.one(RULE, "research-failed").arn),
        "ResearchNotStartedAlarm": (
            "cloudwatch.amazonaws.com",
            jobs.one(ALARM, "research-schedule-dlq").arn,
        ),
    }
    for statement in policy["Statement"]:
        assert (statement["Action"], statement["Resource"]) == ("sns:Publish", jobs.one(TOPIC).arn)


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


# --- C2a: reading the bot's state ------------------------------------------------------------


@pytest.mark.parametrize(("mode", "stack"), [("paper", "dev"), ("live", "prod")])
def test_research_may_only_query_the_bots_ledger_and_event_log(mode, stack):
    config = {"research": True, "researchJobs": True, "alertEmail": "ops@example.test"}
    if mode == "live":
        config |= {"tradingMode": "live", "accountLast4": "5678"}
    deployment = deploy(config, stack=stack)
    read = statements(deployment, "research-task")["ReadBotState"]
    assert (read["Action"], read["Resource"]) == (
        "dynamodb:Query",
        deployment.one(TABLE, "state").arn,
    )
    assert read["Condition"] == {
        "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"POS#{mode}", f"LOG#{mode}#*"]}
    }
    # The namespace the bot itself writes under: its trading mode, the same for every kind.
    bot_env = {
        item["name"]: item["value"]
        for item in json.loads(deployment.one(TASK, "bot").inputs["containerDefinitions"])[0][
            "environment"
        ]
    }
    assert bot_env["TRAIDER_TRADING_MODE"] == mode
    for kind in KINDS:
        env = environment(deployment, kind)
        assert env["TRAIDER_STATE_NAMESPACE"] == mode, kind
        assert env["TRAIDER_STATE_TABLE"] == bot_env["TRAIDER_STATE_TABLE"], kind
    # Nothing else in the research role touches the state table.
    others = [s for s in deployment.policy("research-task") if s["Sid"] != "ReadBotState"]
    assert deployment.one(TABLE, "state").arn not in json.dumps(others)
