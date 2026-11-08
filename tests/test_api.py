from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from webpilot.db import Base
from webpilot.main import app, db_session


def test_task_http_contract():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)

    def test_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[db_session] = test_db
    try:
        with TestClient(app) as client:
            headers = {"Idempotency-Key": "http-create"}
            created = client.post("/api/v1/tasks", json={"query": " 比较 A 与 B "}, headers=headers)
            assert created.status_code == 201
            task = created.json()
            assert task["query"] == "比较 A 与 B"
            again = client.post("/api/v1/tasks", json={"query": "比较 A 与 B"}, headers=headers)
            assert again.json() == task
            bad = client.post("/api/v1/tasks", json={"query": " "})
            assert bad.status_code == 422 and bad.json()["error"]["code"] == "VALIDATION_ERROR"
            assert client.get(f'/api/v1/tasks/{task["id"]}/events').json()["items"][0]["type"] == "task.queued"
            assert client.post(f'/api/v1/tasks/{task["id"]}/cancel', json={}).json()["status"] == "cancelled"
            assert client.post(f'/api/v1/tasks/{task["id"]}/resume', json={}).json()["attempt"] == 2
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
