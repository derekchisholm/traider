"""Run the Pulumi program against mocks and record what it would create.

Nothing here talks to AWS or needs the Pulumi CLI. The mocks check that every
resource is built with argument names the installed providers accept and let the
tests inspect the resulting properties. They do not prove AWS will accept the
values: only ``pulumi preview`` against a real account does that.
"""

from __future__ import annotations

import functools
import json
import sys
from dataclasses import dataclass, field
from typing import Any

import pulumi
import pytest
from pulumi.runtime.rpc import _special_secret_sig, _special_sig_key

ACCOUNT = "123456789012"
REGION = "us-west-2"
REDACTED = "[secret]"


def _is_secret(value: Any) -> bool:
    return isinstance(value, dict) and value.get(_special_sig_key) == _special_secret_sig


def _secret(value: Any) -> dict[str, Any]:
    """A value the way a provider returns one it marks secret."""
    return {_special_sig_key: _special_secret_sig, "value": value}


def reveal(value: Any) -> Any:
    """The plain value: secret wrappers removed, file archives as {name: path}."""
    if _is_secret(value):
        return reveal(value["value"])
    if isinstance(value, pulumi.AssetArchive):
        return {name: reveal(item) for name, item in value.assets.items()}
    if isinstance(value, pulumi.FileAsset):
        return value.path
    if isinstance(value, dict):
        return {key: reveal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [reveal(item) for item in value]
    return value


def redact(value: Any) -> Any:
    """What is left readable once Pulumi has encrypted everything marked secret."""
    if _is_secret(value):
        return REDACTED
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


@dataclass
class Created:
    type: str
    name: str
    inputs: dict[str, Any]  # every value, secrets included
    readable: dict[str, Any]  # the same, minus what Pulumi encrypts in its state
    id: str = ""  # made up by the mock, unique per resource
    outputs: dict[str, Any] = field(default_factory=dict)  # likewise made up
    ignore_changes: list[str] = field(default_factory=list)

    @property
    def arn(self) -> str:
        return self.outputs["arn"]


class Recorder(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.created: list[Created] = []
        self.calls: list[str] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        inputs = reveal(args.inputs)
        created = Created(args.typ, args.name, inputs, redact(args.inputs))
        self.created.append(created)
        kind = args.typ.split(":")[-1]
        created.id = f"{kind}-{args.name}-id"
        physical = inputs.get("name") or f"{args.name}-0a1b2c3"
        service = args.typ.split(":")[1].split("/")[0]
        state = {
            **args.inputs,
            "name": physical,
            "arn": f"arn:aws:{service}:{REGION}:{ACCOUNT}:{kind}/{physical}",
        }
        if args.typ == "aws:apigatewayv2/api:Api":
            state["apiEndpoint"] = "https://abc123.execute-api.us-west-2.amazonaws.com"
            state["executionArn"] = f"arn:aws:execute-api:{REGION}:{ACCOUNT}:abc123"
        if args.typ == "aws:lambda/function:Function":
            state["invokeArn"] = (
                f"arn:aws:apigateway:{REGION}:lambda:path/functions/{state['arn']}/invocations"
            )
        if args.typ == "aws:ecr/repository:Repository":
            state["repositoryUrl"] = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{physical}"
            state["registryId"] = ACCOUNT
        if args.typ == "docker-build:index:Image":
            state["ref"] = f"{inputs['tags'][0]}@sha256:{'ab' * 32}"
            state["digest"] = f"sha256:{'ab' * 32}"
        if args.typ == "random:index/randomPassword:RandomPassword":
            # The real provider marks the generated value secret; so must the mock.
            state["result"] = _secret(f"generated-{args.name}-value")
        created.outputs = reveal(state)
        return [created.id, state]

    def call(self, args: pulumi.runtime.MockCallArgs):
        self.calls.append(args.token)
        if args.token == "aws:index/getAvailabilityZones:getAvailabilityZones":
            names = ["us-west-2a", "us-west-2b", "us-west-2c", "us-west-2d"]
            return {"names": names, "zoneIds": names, "id": REGION}
        if args.token == "aws:ecr/getAuthorizationToken:getAuthorizationToken":
            return {
                "userName": "AWS",
                "password": "ecr-password",
                "proxyEndpoint": f"https://{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com",
                "authorizationToken": "x",
                "expiresAt": "2030-01-01T00:00:00Z",
                "id": REGION,
            }
        if args.token == "aws:index/getRegion:getRegion":
            return {"name": REGION, "region": REGION, "id": REGION}
        if args.token == "aws:index/getCallerIdentity:getCallerIdentity":
            return {
                "accountId": ACCOUNT,
                "arn": f"arn:aws:iam::{ACCOUNT}:user/test",
                "userId": "AIDATEST",
                "id": ACCOUNT,
            }
        raise AssertionError(f"unexpected provider call {args.token}")


@dataclass
class Deployment:
    created: list[Created]
    outputs: dict[str, Any]
    secret_outputs: set[str]

    def of(self, type_token: str) -> list[Created]:
        return [c for c in self.created if c.type == type_token]

    def one(self, type_token: str, name: str | None = None) -> Created:
        matches = [c for c in self.of(type_token) if name is None or c.name == name]
        assert len(matches) == 1, f"expected one {type_token} {name or ''}, found {len(matches)}"
        return matches[0]

    def policy(self, name: str) -> list[dict[str, Any]]:
        document = json.loads(self.one("aws:iam/rolePolicy:RolePolicy", name).inputs["policy"])
        return document["Statement"]


BASE = {"symbols": ["SPY", "QQQ"]}

# Resource options never reach the mocks, so a stack transformation notes the one
# these tests care about. Names repeat across resource types, hence the pair key.
_ignore_changes: dict[tuple[str, str], list[str]] = {}


def _remember(args: pulumi.ResourceTransformationArgs) -> None:
    _ignore_changes[(args.type_, args.name)] = list(args.opts.ignore_changes or [])


@functools.cache
def _watch_options() -> None:
    """Register the transformation once: the mock root stack outlives each deploy."""
    pulumi.runtime.register_stack_transformation(_remember)


def deploy(config: dict[str, Any] | None = None, *, stack: str = "dev") -> Deployment:
    """Run the program with this stack configuration and return what it declared."""
    recorder = Recorder()
    pulumi.runtime.set_mocks(recorder, project="traider", stack=stack, preview=False)
    # pinnedSymbols is the new name for symbols, and setting both is an error, so a test
    # that sets one does not also get the other from BASE.
    given = config or {}
    base = {} if "pinnedSymbols" in given else BASE
    merged = {**base, **given}
    pulumi.runtime.set_all_config(
        {
            "aws:region": REGION,
            **{
                f"traider:{key}": value if isinstance(value, str) else json.dumps(value)
                for key, value in merged.items()
                if value is not None
            },
        }
    )
    _watch_options()
    _ignore_changes.clear()
    outputs: dict[str, Any] = {}
    secret_names: set[str] = set()
    sys.modules.pop("stack", None)

    @pulumi.runtime.test
    async def run():
        import stack as program

        for name, value in program.build().outputs.items():
            output = pulumi.Output.from_input(value)
            outputs[name] = await output.future()
            if await output.is_secret():
                secret_names.add(name)

    run()
    for created in recorder.created:
        created.ignore_changes = _ignore_changes.get((created.type, created.name), [])
    return Deployment(recorder.created, outputs, secret_names)


@pytest.fixture(scope="module")
def paper() -> Deployment:
    """The default deployment: paper trading, market-hours schedule, alerts by email."""
    return deploy({"alertEmail": "ops@example.test"})


@pytest.fixture(scope="module")
def live() -> Deployment:
    return deploy(
        {
            "tradingMode": "live",
            "accountLast4": "5678",
            "alwaysOn": True,
            "alertEmail": "ops@example.test",
        },
        stack="prod",
    )
