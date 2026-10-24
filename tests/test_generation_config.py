from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from emabox.config import Settings
from emabox.providers import GeminiProvider, Message


def test_environment_controls(monkeypatch):
    monkeypatch.setenv("EMABOX_API_CLIENTS", "{}")
    monkeypatch.setenv("EMABOX_GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("EMABOX_GEMINI_MAX_OUTPUT_TOKENS", "384")
    monkeypatch.setenv("EMABOX_GEMINI_THINKING_LEVEL", "minimal")
    monkeypatch.setenv("EMABOX_GEMINI_TEMPERATURE", "1.0")
    settings = Settings.from_env()
    assert settings.gemini_max_output_tokens == 384
    assert settings.gemini_thinking_level == "minimal"
    assert settings.gemini_temperature == 1.0
    for invalid in (0, -1):
        with pytest.raises(ValueError):
            replace(settings, gemini_max_output_tokens=invalid)
    for invalid in (-0.1, 2.1, float("nan")):
        with pytest.raises(ValueError):
            replace(settings, gemini_temperature=invalid)
    with pytest.raises(ValueError):
        replace(settings, gemini_thinking_level="none")
    with pytest.raises(ValueError):
        replace(settings, gemini_model="gemini-2.5-flash")


@pytest.mark.asyncio
async def test_gemini_controls_reach_both_sdk_paths():
    async def chunks():
        yield SimpleNamespace(text="Hi", usage_metadata=None)

    models = SimpleNamespace(
        generate_content=AsyncMock(return_value=SimpleNamespace(text="Hi", usage_metadata=None)),
        generate_content_stream=AsyncMock(side_effect=lambda **kwargs: chunks()),
    )
    with patch("google.genai.Client") as client:
        client.return_value.aio.models = models
        provider = GeminiProvider("fake", "gemini-3.5-flash-lite",
                                  max_output_tokens=512, thinking_level="minimal", temperature=1.0)
    messages = [Message("user", "Hello")]
    await provider.generate("Be brief", messages, 1200)
    result = [item async for item in provider.stream("Be brief", messages, 1200)]
    assert result[0].text == "Hi"
    for method in (models.generate_content, models.generate_content_stream):
        config = method.call_args.kwargs["config"]
        assert config.max_output_tokens == 512
        assert config.thinking_config.thinking_level == "MINIMAL"
        assert config.temperature == 1.0
    assert provider._config("Be brief", 256).max_output_tokens == 256
    provider.thinking_level = None
    provider.temperature = None
    config = provider._config("Be brief", 256)
    assert config.thinking_config is None
    assert config.temperature is None
