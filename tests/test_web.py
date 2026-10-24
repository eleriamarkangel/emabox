from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from emabox.audit import AuditRow, AuditStore
from emabox.web import quota, TAIPEI
from test_api import make_app, FakeProvider


def test_public_pages_and_chat_are_scoped_and_audited(tmp_path):
    app, provider, audit = make_app(tmp_path)
    with TestClient(app) as client:
        for path in ("/", "/chat", "/assets/app.js", "/assets/style.css"):
            assert client.get(path).status_code == 200
        assert client.get("/demo/status").json()["remaining"] == 100
        response = client.post("/demo/chat", json={"prompt": "What is Emabox?"})
        assert "event: delta" in response.text
        assert "event: done" in response.text
        assert client.get("/demo/status").json()["remaining"] == 99
        call = next(item for item in provider.calls if item[0] == "stream")
        assert "Answer only questions about Emabox" in call[1]
        assert call[3] == 2048
        assert not any(item[0] == "count" for item in provider.calls)
        with Session(audit.engine) as session:
            row = session.scalars(select(AuditRow)).one()
            assert row.app_id == "emabox-web" and row.specialist == "emabox"
        assert client.post("/demo/chat", json={"prompt": " "}).status_code == 422
        assert client.post("/demo/chat", headers={"Origin": "https://other.example"},
                           json={"prompt": "hello"}).status_code == 403
        assert client.get("/demo/status").json()["remaining"] == 99


def test_quota_is_atomic_persistent_and_resets_at_taipei_midnight(tmp_path):
    url = f"sqlite:///{tmp_path / 'quota.db'}"
    store = AuditStore(url, 60)
    store.create()
    now = datetime(2026, 10, 24, 23, 59, tzinfo=TAIPEI)
    def reserve(_):
        try:
            quota(store.engine, reserve=True, now=now)
            return True
        except HTTPException as error:
            assert error.status_code == 429
            return False
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(reserve, range(110))) == 100
    store.close()
    reopened = AuditStore(url, 60)
    assert quota(reopened.engine, now=now)["remaining"] == 0
    assert quota(reopened.engine, reserve=True, now=now+timedelta(minutes=1))["remaining"] == 99
    reopened.close()


def test_exhausted_quota_never_calls_model(tmp_path):
    app, provider, audit = make_app(tmp_path)
    with TestClient(app) as client:
        for _ in range(100):
            quota(audit.engine, reserve=True)
        assert client.post("/demo/chat", json={"prompt": "Hello"}).status_code == 429
        assert provider.calls == []


def test_failed_generation_consumes_slot(tmp_path):
    class BrokenProvider(FakeProvider):
        async def stream(self, *args, **kwargs):
            raise RuntimeError("provider failed")
            yield
    app, provider, audit = make_app(tmp_path, BrokenProvider())
    with TestClient(app) as client:
        response = client.post("/demo/chat", json={"prompt": "Hello"})
        assert "event: error" in response.text
        assert "event: done" not in response.text
        assert client.get("/demo/status").json()["remaining"] == 99
        with Session(audit.engine) as session:
            assert session.scalars(select(AuditRow)).one().status == "error"
