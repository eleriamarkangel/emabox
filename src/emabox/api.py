"""Public HTTP API for application callers."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .audit import AuditEvent, AuditStore
from .config import ClientConfig, Settings, Specialist
from .providers import Message, Provider, Usage, create_provider
from .routing import INSTRUCTIONS, choose_specialist

logger = logging.getLogger(__name__)


class ContextMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=50_000)


class GenerationRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=50_000)
    goal: str | None = Field(default=None, max_length=500)
    messages: list[ContextMessage] = Field(default_factory=list, max_length=30)
    specialist: Specialist | None = None
    stream: bool = False


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(
    settings: Settings | None = None,
    provider: Provider | None = None,
    audit: AuditStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.validate_ready()
        model_provider = provider or create_provider(settings)
        audit_store = audit or AuditStore(settings.database_url, settings.log_retention_days)
        await asyncio.to_thread(audit_store.create)
        app.state.provider = model_provider
        app.state.audit = audit_store
        app.state.web_provider = provider or (create_provider(replace(
            settings, provider="gemini", gemini_max_output_tokens=2048,
            gemini_thinking_level="low" if settings.gemini_model.startswith("gemini-3") else None,
            gemini_temperature=None,
        )) if settings.gemini_api_key else None)
        stop = asyncio.Event()

        async def periodic_purge() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=24 * 60 * 60)
                except TimeoutError:
                    try:
                        await asyncio.to_thread(audit_store.purge)
                    except Exception:
                        logger.exception("Audit retention cleanup failed")

        purge_task = asyncio.create_task(periodic_purge())
        try:
            yield
        finally:
            stop.set()
            await purge_task
            await model_provider.close()
            if app.state.web_provider is not None and app.state.web_provider is not model_provider:
                await app.state.web_provider.close()
            await asyncio.to_thread(audit_store.close)

    app = FastAPI(title="Emabox AI API", version="0.1.0", lifespan=lifespan)

    def authenticate(
        x_api_key: str | None = Header(
            default=None,
            alias="X-API-Key",
            description=(
                "Emabox application key from EMABOX_API_CLIENTS. "
                "This is separate from your Gemini or OpenAI provider key."
            ),
        ),
    ) -> tuple[str, ClientConfig]:
        if x_api_key is None:
            raise HTTPException(status_code=401, detail="Invalid application API key")
        for app_id, client in settings.clients.items():
            if secrets.compare_digest(x_api_key, client.key):
                return app_id, client
        raise HTTPException(status_code=401, detail="Invalid application API key")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "provider": settings.provider, "model": settings.model}

    @app.post("/v1/responses")
    async def generate(
        body: GenerationRequest,
        caller: tuple[str, ClientConfig] = Depends(authenticate),
    ):
        app_id, client = caller
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        specialist = choose_specialist(
            body.prompt, body.goal, body.specialist, client.default_specialist
        )
        instructions = INSTRUCTIONS[specialist]
        messages = [Message(item.role, item.content) for item in body.messages]
        current_prompt = f"Goal: {body.goal}\n\nPrompt: {body.prompt}" if body.goal else body.prompt
        messages.append(Message("user", current_prompt))
        max_output = (
            settings.generic_max_output_tokens if specialist == "generic"
            else settings.specialist_max_output_tokens
        )

        async def record(status: str, usage: Usage | None = None) -> None:
            event = AuditEvent(
                request_id=request_id,
                app_id=app_id,
                specialist=specialist,
                provider=settings.provider,
                model=settings.model,
                streamed=body.stream,
                status=status,
                latency_ms=round((time.perf_counter() - started) * 1000),
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
            )
            await asyncio.to_thread(app.state.audit.write, event)

        # Keep a local size ceiling before calling the provider's exact token counter.
        total_chars = len(instructions) + sum(len(item.content) for item in messages)
        if total_chars > 100_000:
            await record("rejected")
            raise HTTPException(status_code=413, detail="Request context is too large")

        if specialist == "generic":
            try:
                input_tokens = await app.state.provider.count_input_tokens(instructions, messages)
            except Exception:
                logger.error("Input token counting failed for request %s", request_id)
                await record("error")
                raise HTTPException(status_code=502, detail={"request_id": request_id, "error": "Model provider unavailable"})
            if input_tokens > settings.generic_max_input_tokens:
                await record("rejected", Usage(input_tokens=input_tokens))
                raise HTTPException(
                    status_code=413,
                    detail={
                        "request_id": request_id,
                        "error": "Generic input token limit exceeded",
                        "limit": settings.generic_max_input_tokens,
                    },
                )

        if body.stream:
            async def events():
                status = "ok"
                usage = Usage()
                try:
                    yield _sse("meta", {
                        "request_id": request_id, "specialist": specialist,
                        "provider": settings.provider, "model": settings.model,
                    })
                    async for item in app.state.provider.stream(
                        instructions, messages, max_output,
                        economical=specialist == "generic",
                    ):
                        if item.text:
                            yield _sse("delta", {"text": item.text})
                        if item.usage is not None:
                            usage = item.usage
                    yield _sse("done", {
                        "request_id": request_id,
                        "usage": {"input_tokens": usage.input_tokens,
                                  "output_tokens": usage.output_tokens},
                    })
                except asyncio.CancelledError:
                    status = "cancelled"
                    raise
                except Exception:
                    status = "error"
                    logger.error("Streaming generation failed for request %s", request_id)
                    yield _sse("error", {"request_id": request_id, "error": "Model provider unavailable"})
                finally:
                    await record(status, usage)

            return StreamingResponse(
                events(), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            result = await app.state.provider.generate(
                instructions, messages, max_output,
                economical=specialist == "generic",
            )
            if not result.text.strip():
                raise RuntimeError("Model returned no text")
        except Exception:
            logger.error("Generation failed for request %s", request_id)
            await record("error")
            raise HTTPException(status_code=502, detail={"request_id": request_id, "error": "Model provider unavailable"})

        await record("ok", result.usage)
        return JSONResponse({
            "request_id": request_id,
            "specialist": specialist,
            "provider": settings.provider,
            "model": settings.model,
            "response": result.text,
            "usage": {"input_tokens": result.usage.input_tokens,
                      "output_tokens": result.usage.output_tokens},
        })

    from .web import install_web
    install_web(app, settings)
    return app
