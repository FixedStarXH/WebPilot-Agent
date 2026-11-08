from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from webpilot.db import Base
from webpilot.models import Task, TaskEvent, utcnow
from webpilot.service import ServiceError, cancel_task, create_task, resume_task
from webpilot.worker import claim, heartbeat, recover_expired


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_create_idempotency_and_conflict(db):
    first = create_task(db, "比较 A 和 B", 10, "same-key")
    assert create_task(db, "比较 A 和 B", 10, "same-key") == first
    assert db.query(Task).count() == 1
    with pytest.raises(ServiceError) as exc:
        create_task(db, "比较别的", 10, "same-key")
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_cancel_and_resume(db):
    task = create_task(db, "比较 A 和 B", 10)
    assert cancel_task(db, task["id"])["status"] == "cancelled"
    resumed = resume_task(db, task["id"], "retry")
    assert resumed["attempt"] == 2 and resumed["status"] == "queued"
    assert resume_task(db, task["id"], "retry") == resumed
    assert [e.type for e in db.scalars(select(TaskEvent).order_by(TaskEvent.sequence))] == [
        "task.queued", "task.cancelled", "task.queued"]


def test_expired_worker_and_stale_token(db):
    created = create_task(db, "比较 A 和 B", 10)
    task_id, token = claim(db, "worker-a")
    assert task_id == created["id"]
    assert heartbeat(db, task_id, token)
    task = db.get(Task, task_id)
    task.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert recover_expired(db) == 1
    assert db.get(Task, task_id).status == "failed"
    with pytest.raises(ServiceError) as exc:
        heartbeat(db, task_id, token)
    assert exc.value.code == "LEASE_LOST"
