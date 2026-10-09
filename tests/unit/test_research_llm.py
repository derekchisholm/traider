"""The Bedrock client wrapper, without the network, and the scripted stand-in."""

import anthropic
import httpx2
import pytest
from anthropic import AsyncAnthropicBedrockMantle
from anthropic.types import Message

from tests.fakes.research import ScriptedLLM, posture_reply, reply, tool_use
from traider.research.llm import LLMError, LLMReply, MantleLLM, Usage

REQUEST = {
    "model": "anthropic.claude-sonnet-5-5",
    "system": "You are a test.",
    "messages": [{"role": "user", "content": "Symbol: NVDA\nhello"}],
    "tools": [{"name": "daily_bars", "description": "d", "input_schema": {"type": "object"}}],
    "tool_choice": {"type": "any"},
    "max_tokens": 100,
}


class FakeMessages:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result):
        self.messages = FakeMessages(result)


MESSAGE = Message.model_validate(
    {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "anthropic.claude-sonnet-5-5",
        "content": [
            {"type": "text", "text": "Looking at the bars."},
            {"type": "tool_use", "id": "toolu_1", "name": "daily_bars", "input": {"days": 20}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 1234, "output_tokens": 56},
    }
)


def test_the_mantle_client_is_built_for_the_region_without_touching_the_network():
    client = AsyncAnthropicBedrockMantle(aws_region="us-west-2")
    assert str(client.base_url) == "https://bedrock-mantle.us-west-2.api.aws/anthropic/"
    assert isinstance(MantleLLM("us-west-2")._client, AsyncAnthropicBedrockMantle)


async def test_a_reply_becomes_plain_content_blocks_and_usage():
    fake = FakeClient(MESSAGE)
    got = await MantleLLM("us-west-2", client=fake).create(**REQUEST)
    assert got == LLMReply(
        content=(
            {"type": "text", "text": "Looking at the bars."},
            {"type": "tool_use", "id": "toolu_1", "name": "daily_bars", "input": {"days": 20}},
        ),
        usage=Usage(1234, 56),
        stop_reason="tool_use",
    )
    assert got.tool_uses() == [got.content[1]]
    (sent,) = fake.messages.calls
    assert sent == REQUEST


async def test_api_errors_become_llm_errors_without_details():
    request = httpx2.Request(
        "POST", "https://bedrock-mantle.us-west-2.api.aws/anthropic/v1/messages"
    )
    response = httpx2.Response(403, request=request, json={"message": "no access"})
    refused = anthropic.PermissionDeniedError("denied", response=response, body=None)
    with pytest.raises(LLMError, match=r"HTTP 403"):
        await MantleLLM("us-west-2", client=FakeClient(refused)).create(**REQUEST)
    lost = anthropic.APIConnectionError(request=request)
    with pytest.raises(LLMError, match="APIConnectionError"):
        await MantleLLM("us-west-2", client=FakeClient(lost)).create(**REQUEST)


async def test_the_scripted_llm_routes_by_symbol_and_records_requests():
    llm = ScriptedLLM(
        posture=[posture_reply("reduced", "rates")],
        dives={"NVDA": [reply(tool_use("daily_bars", {"days": 5}))]},
    )
    first = await llm.create(**REQUEST)
    assert first.tool_uses()[0]["name"] == "daily_bars"
    posture = await llm.create(
        **{
            **REQUEST,
            "tools": [{"name": "submit_posture"}],
            "messages": [{"role": "user", "content": "{}"}],
        }
    )
    assert posture.tool_uses()[0]["input"]["level"] == "reduced"
    assert len(llm.requests_for("NVDA")) == 1
    with pytest.raises(LLMError, match="exhausted"):
        await llm.create(**REQUEST)
