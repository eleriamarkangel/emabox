"""Environment-backed configuration. Secrets are never stored in the audit database."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal

Specialist = Literal["journal", "it", "social", "generic"]


@dataclass(frozen=True)
class ClientConfig:
    key: str
    default_specialist: Specialist | None = None


@dataclass(frozen=True)
class Settings:
    provider: Literal["gemini", "openai"]
    gemini_api_key: str | None
    openai_api_key: str | None
    gemini_model: str
    openai_model: str
    database_url: str
    clients: dict[str, ClientConfig]
    log_retention_days: int = 40
    generic_max_input_tokens: int = 1200
    generic_max_output_tokens: int = 256
    specialist_max_output_tokens: int = 1200

    @property
    def model(self) -> str:
        return self.gemini_model if self.provider == "gemini" else self.openai_model

    @classmethod
    def from_env(cls) -> "Settings":
        provider = os.getenv("EMABOX_PROVIDER", "gemini").lower()
        if provider not in {"gemini", "openai"}:
            raise ValueError("EMABOX_PROVIDER must be gemini or openai")

        raw_clients = json.loads(os.getenv("EMABOX_API_CLIENTS", "{}"))
        if not isinstance(raw_clients, dict):
            raise ValueError("EMABOX_API_CLIENTS must be a JSON object")
        clients: dict[str, ClientConfig] = {}
        for app_id, data in raw_clients.items():
            if not isinstance(app_id, str) or not app_id or not isinstance(data, dict):
                raise ValueError("Invalid EMABOX_API_CLIENTS entry")
            key = data.get("key")
            default = data.get("default_specialist")
            if not isinstance(key, str) or len(key) < 16:
                raise ValueError(f"API key for {app_id} must be at least 16 characters")
            if default is not None and default not in {"journal", "it", "social", "generic"}:
                raise ValueError(f"Invalid default_specialist for {app_id}")
            clients[app_id] = ClientConfig(key=key, default_specialist=default)

        retention = int(os.getenv("EMABOX_LOG_RETENTION_DAYS", "40"))
        if retention < 1:
            raise ValueError("EMABOX_LOG_RETENTION_DAYS must be positive")
        return cls(
            provider=provider,
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            gemini_model=os.getenv("EMABOX_GEMINI_MODEL", "gemini-3.8-flash"),
            openai_model=os.getenv("EMABOX_OPENAI_MODEL", "gpt-6-luna"),
            database_url=os.getenv("EMABOX_DATABASE_URL", "sqlite:///./emabox.db"),
            clients=clients,
            log_retention_days=retention,
        )

    def validate_ready(self) -> None:
        if not self.clients:
            raise ValueError("Configure at least one application in EMABOX_API_CLIENTS")
        if self.provider == "gemini" and not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required for Gemini")
        if self.provider == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for OpenAI")
