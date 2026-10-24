"""Thin adapters around the official Gemini and OpenAI Python SDKs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from .config import Settings


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage


@dataclass(frozen=True)
class StreamItem:
    text: str = ""
    usage: Usage | None = None


class Provider(Protocol):
    async def count_input_tokens(self, instructions: str, messages: list[Message]) -> int: ...
    async def generate(
        self, instructions: str, messages: list[Message], max_output_tokens: int,
        *, economical: bool = False,
    ) -> Completion: ...
    def stream(
        self, instructions: str, messages: list[Message], max_output_tokens: int,
        *, economical: bool = False,
    ) -> AsyncIterator[StreamItem]: ...
    async def close(self) -> None: ...


class GeminiProvider:
    def __init__(self, api_key: str, model: str, *, max_output_tokens: int = 512,
                 thinking_level: str | None = None, temperature: float | None = None):
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.thinking_level = thinking_level
        self.temperature = temperature

    def _config(self, instructions: str, max_output_tokens: int):
        from google.genai import types

        options = {
            "system_instruction": instructions,
            "max_output_tokens": min(max_output_tokens, self.max_output_tokens),
        }
        if self.thinking_level is not None:
            options["thinking_config"] = types.ThinkingConfig(
                thinking_level=self.thinking_level.upper(),
            )
        if self.temperature is not None:
            options["temperature"] = self.temperature
        return types.GenerateContentConfig(**options)

    @staticmethod
    def _contents(messages: list[Message]) -> list[dict]:
        return [
            {"role": "model" if item.role == "assistant" else "user",
             "parts": [{"text": item.content}]}
            for item in messages
        ]

    @staticmethod
    def _usage(metadata) -> Usage:
        if metadata is None:
            return Usage()
        input_tokens = getattr(metadata, "prompt_token_count", None)
        output_tokens = getattr(metadata, "candidates_token_count", None)
        thoughts = getattr(metadata, "thoughts_token_count", None)
        if output_tokens is not None and thoughts is not None:
            output_tokens += thoughts
        elif output_tokens is None and input_tokens is not None:
            total = getattr(metadata, "total_token_count", None)
            output_tokens = total - input_tokens if total is not None else None
        return Usage(input_tokens=input_tokens, output_tokens=output_tokens)

    async def count_input_tokens(self, instructions: str, messages: list[Message]) -> int:
        from google.genai import types

        result = await self.client.aio.models.count_tokens(
            model=self.model,
            contents=self._contents(messages),
            config=types.CountTokensConfig(system_instruction=instructions),
        )
        if result.total_tokens is None:
            raise RuntimeError("Gemini did not return an input token count")
        return result.total_tokens

    async def generate(
        self, instructions: str, messages: list[Message], max_output_tokens: int,
        *, economical: bool = False,
    ) -> Completion:
        from google.genai import types

        result = await self.client.aio.models.generate_content(
            model=self.model,
            contents=self._contents(messages),
            config=self._config(instructions, max_output_tokens),
        )
        return Completion(text=result.text or "", usage=self._usage(result.usage_metadata))

    async def stream(
        self, instructions: str, messages: list[Message], max_output_tokens: int,
        *, economical: bool = False,
    ) -> AsyncIterator[StreamItem]:
        from google.genai import types

        chunks = await self.client.aio.models.generate_content_stream(
            model=self.model,
            contents=self._contents(messages),
            config=self._config(instructions, max_output_tokens),
        )
        usage = Usage()
        async for chunk in chunks:
            if chunk.text:
                yield StreamItem(text=chunk.text)
            metadata = chunk.usage_metadata
            if metadata:
                usage = self._usage(metadata)
        yield StreamItem(usage=usage)

    async def close(self) -> None:
        await self.client.aio.aclose()
        self.client.close()


class OpenAIProvider:
    def __init__(self, api_key: str, model: str):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    @staticmethod
    def _input(messages: list[Message]) -> list[dict]:
        return [{"role": item.role, "content": item.content} for item in messages]

    async def count_input_tokens(self, instructions: str, messages: list[Message]) -> int:
        result = await self.client.responses.input_tokens.count(
            model=self.model, instructions=instructions, input=self._input(messages)
        )
        return result.input_tokens

    def _params(
        self, instructions: str, messages: list[Message], max_output_tokens: int,
        economical: bool,
    ) -> dict:
        params = {
            "model": self.model,
            "instructions": instructions,
            "input": self._input(messages),
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if economical:
            params["reasoning"] = {"effort": "none"}
        return params

    async def generate(
        self, instructions: str, messages: list[Message], max_output_tokens: int,
        *, economical: bool = False,
    ) -> Completion:
        result = await self.client.responses.create(
            **self._params(instructions, messages, max_output_tokens, economical)
        )
        usage = result.usage
        return Completion(
            text=result.output_text or "",
            usage=Usage(
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
            ),
        )

    async def stream(
        self, instructions: str, messages: list[Message], max_output_tokens: int,
        *, economical: bool = False,
    ) -> AsyncIterator[StreamItem]:
        stream = await self.client.responses.create(
            **self._params(instructions, messages, max_output_tokens, economical),
            stream=True,
        )
        usage = Usage()
        async for event in stream:
            if event.type == "response.output_text.delta":
                yield StreamItem(text=event.delta)
            elif event.type == "response.completed":
                actual = event.response.usage
                usage = Usage(
                    input_tokens=getattr(actual, "input_tokens", None),
                    output_tokens=getattr(actual, "output_tokens", None),
                )
            elif event.type == "response.failed":
                raise RuntimeError("OpenAI response failed")
        yield StreamItem(usage=usage)

    async def close(self) -> None:
        await self.client.close()


def create_provider(settings: Settings) -> Provider:
    settings.validate_ready()
    if settings.provider == "gemini":
        return GeminiProvider(
            settings.gemini_api_key or "", settings.gemini_model,
            max_output_tokens=settings.gemini_max_output_tokens,
            thinking_level=settings.gemini_thinking_level,
            temperature=settings.gemini_temperature,
        )
    return OpenAIProvider(settings.openai_api_key or "", settings.openai_model)
