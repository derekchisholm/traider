"""Claude on Amazon Bedrock, through the Anthropic Messages API (the ``bedrock-mantle``
endpoint), signed with the task role's credentials.

Research only needs one call: ``create``. Requests and replies use the Messages API's own
shapes as plain dicts (content blocks such as ``{"type": "tool_use", ...}``), so a scripted
fake can stand in for the model in tests. Structured outputs are not offered on that
endpoint: callers force a tool with ``tool_choice`` and validate what comes back.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

import anthropic
from anthropic import AsyncAnthropicBedrockMantle

# Input tokens added to every request estimate that forces a tool. With tools, the Messages
# API puts its own tool-use system prompt in front of ours; it is not in what we send, and
# its size on Bedrock is unmeasured. Generous on purpose: an estimate must be an upper bound.
TOOL_OVERHEAD_TOKENS: Final = 1000


class LLMError(Exception):
    """The model could not be asked, or did not answer. Never contains credentials."""


@dataclass(frozen=True, slots=True)
class Usage:
    # Cache token fields are not counted: no cache_control is sent, so the API reports none.
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class LLMReply:
    content: tuple[dict[str, Any], ...]
    usage: Usage
    stop_reason: str | None = None

    def tool_uses(self) -> list[dict[str, Any]]:
        return [block for block in self.content if block.get("type") == "tool_use"]


class LLM(Protocol):
    async def create(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        tool_choice: Mapping[str, Any],
        max_tokens: int,
    ) -> LLMReply: ...


def _block(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Only what a later request may send back: text and tool calls, nothing else."""
    if raw.get("type") == "text":
        return {"type": "text", "text": str(raw.get("text", ""))}
    if raw.get("type") == "tool_use":
        tool_input = raw.get("input")
        return {
            "type": "tool_use",
            "id": str(raw.get("id", "")),
            "name": str(raw.get("name", "")),
            "input": tool_input if isinstance(tool_input, dict) else {},
        }
    return None


class MantleLLM:
    """``LLM`` over ``AsyncAnthropicBedrockMantle`` (``anthropic[bedrock]`` 1.13 or later)."""

    def __init__(
        self,
        region: str,
        *,
        timeout_s: float = 120.0,
        client: Any = None,
    ) -> None:
        # No SDK retries: each metered call is exactly one HTTP attempt, so a retry can
        # never bill tokens the cost meter did not see.
        self._client = client or AsyncAnthropicBedrockMantle(
            aws_region=region, timeout=timeout_s, max_retries=0
        )

    async def create(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        tool_choice: Mapping[str, Any],
        max_tokens: int,
    ) -> LLMReply:
        try:
            message = await self._client.messages.create(
                model=model,
                system=system,
                messages=cast(Any, [dict(m) for m in messages]),
                tools=cast(Any, [dict(t) for t in tools]),
                tool_choice=cast(Any, dict(tool_choice)),
                max_tokens=max_tokens,
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Bedrock refused the request (HTTP {exc.status_code})") from None
        except anthropic.AnthropicError as exc:
            raise LLMError(f"Bedrock could not be asked ({type(exc).__name__})") from None
        except Exception as exc:
            raise LLMError(type(exc).__name__) from None  # never the message text
        blocks = [_block(block.model_dump(mode="json")) for block in message.content]
        return LLMReply(
            content=tuple(b for b in blocks if b is not None),
            usage=Usage(message.usage.input_tokens, message.usage.output_tokens),
            stop_reason=message.stop_reason,
        )
