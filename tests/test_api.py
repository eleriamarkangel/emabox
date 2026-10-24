from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from emabox.api import create_app
from emabox.audit import AuditRow, AuditStore
from emabox.config import ClientConfig, Settings
from emabox.providers import Completion, Message, StreamItem, Usage
from emabox.routing import choose_specialist


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.tokens = 42

    async def count_input_tokens(self, instructions, messages):
        self.calls.append(("count", instructions, messages))
        return self.tokens

    async def generate(self, instructions, messages, max_output_tokens, *, economical=False):
        self.calls.append(("generate", instructions, messages, max_output_tokens, economical))
        return Completion("A useful answer.", Usage(42, 12))

    async def stream(self, instructions, messages, max_output_tokens, *, economical=False):
        self.calls.append(("stream", instructions, messages, max_output_tokens, economical))
        yield StreamItem(text="Hello ")
        yield StreamItem(text="there")
        yield StreamItem(usage=Usage(42, 2))

    async def close(self):
        pass


def make_app(tmp_path, provider=None):
    provider = provider or FakeProvider()
    settings = Settings(
        provider="gemini",
        gemini_api_key="test-gemini-key",
        openai_api_key=None,
        gemini_model="gemini-test",
        openai_model="gpt-6-luna",
        database_url=f"sqlite:///{tmp_path / 'audit.db'}",
        clients={
            "diary": ClientConfig("diary-key-12345678", "journal"),
            "other": ClientConfig("other-key-12345678"),
        },
    )
    audit = AuditStore(settings.database_url, settings.log_retention_days)
    return create_app(settings, provider, audit), provider, audit


def test_auth_and_journal_context_are_scoped_to_request(tmp_path):
    app, provider, audit = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/v1/responses", json={"prompt": "hello"}).status_code == 401
        assert client.post("/v1/responses", headers={"X-API-Key": "wrong"}, json={"prompt": "hello"}).status_code == 401
        response = client.post(
            "/v1/responses",
            headers={"X-API-Key": "diary-key-12345678"},
            json={"prompt": "Today was difficult.", "messages": [
                {"role": "user", "content": "Yesterday was stressful."},
                {"role": "assistant", "content": "That sounds heavy."},
            ]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["specialist"] == "journal"
        assert body["response"] == "A useful answer."
        call = next(call for call in provider.calls if call[0] == "generate")
        assert call[2] == [
            Message("user", "Yesterday was stressful."),
            Message("assistant", "That sounds heavy."),
            Message("user", "Today was difficult."),
        ]
        assert "Do not diagnose" in call[1]
        assert "ask follow-up questions" in call[1]
        with Session(audit.engine) as session:
            rows = session.scalars(select(AuditRow)).all()
            assert len(rows) == 1
            assert rows[0].app_id == "diary"
            assert rows[0].status == "ok"
            columns = {column["name"] for column in inspect(audit.engine).get_columns("request_audit")}
            assert not {"prompt", "response", "messages", "goal"} & columns


def test_routing_and_generic_limits(tmp_path):
    app, provider, audit = make_app(tmp_path)
    with TestClient(app) as client:
        headers = {"X-API-Key": "other-key-12345678"}
        it = client.post("/v1/responses", headers=headers, json={"prompt": "Fix this Python error"})
        social = client.post("/v1/responses", headers=headers, json={"prompt": "Write an Instagram caption"})
        generic = client.post("/v1/responses", headers=headers, json={"prompt": "Explain this simply"})
        forced = client.post("/v1/responses", headers=headers, json={
            "prompt": "Explain this simply", "specialist": "social"
        })
        assert [x.json()["specialist"] for x in (it, social, generic, forced)] == [
            "it", "social", "generic", "social"
        ]
        generic_call = [x for x in provider.calls if x[0] == "generate"][2]
        assert generic_call[3] == 256
        assert generic_call[4] is True
        assert len([x for x in provider.calls if x[0] == "count"]) == 1

        provider.tokens = 1201
        too_large = client.post("/v1/responses", headers=headers, json={"prompt": "A generic request"})
        assert too_large.status_code == 413
        with Session(audit.engine) as session:
            assert session.scalars(select(AuditRow).where(AuditRow.status == "rejected")).one()


def test_streaming_returns_events_and_logs_usage(tmp_path):
    app, provider, audit = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses", headers={"X-API-Key": "other-key-12345678"},
            json={"prompt": "Write an Instagram caption", "stream": True},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: meta" in response.text
        assert 'event: delta\ndata: {"text": "Hello "}' in response.text
        assert "event: done" in response.text
        with Session(audit.engine) as session:
            row = session.scalars(select(AuditRow)).one()
            assert row.streamed is True
            assert row.output_tokens == 2


def test_retention_removes_old_metadata(tmp_path):
    app, provider, audit = make_app(tmp_path)
    with TestClient(app):
        with Session(audit.engine) as session:
            session.add(AuditRow(
                request_id="old", created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=61),
                app_id="other", specialist="generic", provider="gemini", model="test",
                streamed=False, status="ok", latency_ms=1,
            ))
            session.add(AuditRow(
                request_id="recent", created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=59),
                app_id="other", specialist="generic", provider="gemini", model="test",
                streamed=False, status="ok", latency_ms=1,
            ))
            session.commit()
        assert audit.purge() == 1
        with Session(audit.engine) as session:
            assert [row.request_id for row in session.scalars(select(AuditRow))] == ["recent"]


def test_goal_is_primary_routing_signal():
    assert choose_specialist("Write some code", goal="Create an Instagram campaign") == "social"
    assert choose_specialist("I am angry", goal="Reflect on my diary") == "journal"
    assert choose_specialist("I spent all day fixing a Python error", app_default="journal") == "journal"
    assert choose_specialist("A greeting") == "generic"


def test_goal_reaches_model_as_user_context(tmp_path):
    app, provider, _ = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses", headers={"X-API-Key": "other-key-12345678"},
            json={"prompt": "Rain", "goal": "Write an Instagram caption"},
        )
        assert response.status_code == 200
        assert response.json()["specialist"] == "social"
        call = next(call for call in provider.calls if call[0] == "generate")
        assert call[2][-1] == Message("user", "Goal: Write an Instagram caption\n\nPrompt: Rain")
