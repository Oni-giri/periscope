"""Anthropic JSON completion wrapper plus deterministic test doubles."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from periscope.config import ModelConfig
from periscope.db import Database


class LLMError(RuntimeError):
    """Base exception for LLM configuration and request failures."""


class LLMResponseError(LLMError):
    """Raised when a model response does not contain usable JSON."""


class JSONLLM(Protocol):
    async def complete_json(
        self,
        task: str,
        *,
        system_prompt: str,
        payload: Mapping[str, Any],
        model: str,
    ) -> Any: ...


def load_prompt(path: Path | None, name: str) -> str:
    if path is None:
        raise LLMError(f"The {name} prompt path is not configured")
    if not path.exists():
        raise LLMError(f"The {name} prompt does not exist yet: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise LLMError(f"The {name} prompt is empty: {path}")
    return text


def parse_json_response(text: str) -> Any:
    """Parse JSON with defensive Markdown-fence and surrounding-text handling."""

    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        value = "\n".join(lines).strip()

    starts = [index for index in (value.find("{"), value.find("[")) if index >= 0]
    if not starts:
        raise LLMResponseError("Model response did not contain a JSON object or array")
    try:
        parsed, _ = json.JSONDecoder().raw_decode(value[min(starts) :])
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"Model returned malformed JSON: {exc.msg}") from exc
    if not isinstance(parsed, (dict, list)):
        raise LLMResponseError("Model JSON must be an object or array")
    return parsed


class AnthropicJSONLLM:
    def __init__(
        self,
        api_key: str,
        *,
        database: Database,
        models: ModelConfig,
    ):
        self.api_key = api_key
        self.database = database
        self.models = models
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - only without runtime deps
                raise LLMError("anthropic is not installed; run uv sync") from exc
            self._client = AsyncAnthropic(api_key=self.api_key, timeout=120.0)
        return self._client

    async def complete_json(
        self,
        task: str,
        *,
        system_prompt: str,
        payload: Mapping[str, Any],
        model: str,
    ) -> Any:
        try:
            message = await self._get_client().messages.create(
                model=model,
                max_tokens=self.models.max_tokens,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    }
                ],
            )
        except Exception as exc:
            raise LLMError(f"Anthropic {task} request failed: {exc}") from exc

        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        input_tokens = int(getattr(message.usage, "input_tokens", 0))
        output_tokens = int(getattr(message.usage, "output_tokens", 0))
        input_rate, output_rate = self.models.rates_for(model)
        usd = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        self.database.record_llm_spend(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=usd,
        )
        return parse_json_response(text)


class StaticJSONLLM:
    """Return predefined values, useful for defensive parser tests."""

    def __init__(self, responses: Mapping[str, Any]):
        self.responses = dict(responses)

    async def complete_json(
        self,
        task: str,
        *,
        system_prompt: str,
        payload: Mapping[str, Any],
        model: str,
    ) -> Any:
        response = self.responses.get(task, [])
        if isinstance(response, Exception):
            raise response
        if isinstance(response, str):
            return parse_json_response(response)
        return response


class DeterministicJSONLLM:
    """A credential-free structural stand-in for exercising the whole pipeline."""

    _groups = (
        ("Databases", re.compile(r"\b(sqlite|postgres|database|query|replication)\b", re.I)),
        ("AI", re.compile(r"\b(model|llm|interpret\w*|feature|evals?|training)\b", re.I)),
        ("Homelab", re.compile(r"\b(homelab|tailscale|self-host|container)\b", re.I)),
    )
    _artifact = re.compile(
        r"\b(releas(?:e|ed)|launch(?:ed)?|open.source|dataset|tool|library)\b", re.I
    )

    async def complete_json(
        self,
        task: str,
        *,
        system_prompt: str,
        payload: Mapping[str, Any],
        model: str,
    ) -> Any:
        tweets = payload.get("tweets", [])
        if not isinstance(tweets, list):
            return []

        if task == "clusters":
            grouped: dict[str, list[Mapping[str, Any]]] = {}
            for tweet in tweets:
                if not isinstance(tweet, Mapping):
                    continue
                text = str(tweet.get("text", ""))
                group = next(
                    (name for name, pattern in self._groups if pattern.search(text)),
                    "Other",
                )
                grouped.setdefault(group, []).append(tweet)
            result = []
            for group, items in grouped.items():
                if group == "Other" and len(items) == 1:
                    continue
                result.append(
                    {
                        "headline": f"{group} updates",
                        "synthesis": (
                            f"{len(items)} related posts were collected in this fetch window."
                        ),
                        "tweet_ids": [str(item["id"]) for item in items],
                        "tag": group.lower(),
                    }
                )
            return result

        if task == "picks":
            maximum = int(payload.get("maximum", 5))
            picks = []
            for tweet in tweets:
                if not isinstance(tweet, Mapping) or not self._artifact.search(
                    str(tweet.get("text", ""))
                ):
                    continue
                picks.append(
                    {
                        "tweet_id": str(tweet["id"]),
                        "tag": "ARTIFACT",
                        "reason": "Concrete release worth opening",
                    }
                )
                if len(picks) >= maximum:
                    break
            return picks

        return []
