import hashlib
import json
from datetime import timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import IdempotencyRecord, Task, TaskEvent, uid, utcnow


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int):
        self.code, self.message, self.status = code, message, status


def event(db: Session, task: Task, kind: str, payload: dict):
    seq = db.scalar(select(func.max(TaskEvent.sequence)).where(TaskEvent.task_id == task.id)) or 0
    db.add(TaskEvent(task_id=task.id, sequence=seq + 1, attempt=task.attempt, type=kind, payload=payload))


def task_json(t: Task) -> dict:
    def timestamp(value):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()

    return {
        "id": t.id, "query": t.query, "status": t.status, "options": t.options,
        "attempt": t.attempt, "cancel_requested": t.cancel_requested,
        "created_at": timestamp(t.created_at), "updated_at": timestamp(t.updated_at),
        "error": {"code": t.error_code, "message": t.error_message} if t.error_code else None,
    }


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def replay(db: Session, operation: str, scope: str, key: str | None, request: dict):
    if not key:
        return None
    record = db.get(IdempotencyRecord, (operation, scope, key))
    if record and record.expires_at.replace(tzinfo=record.expires_at.tzinfo or timezone.utc) > utcnow():
        if record.request_hash != fingerprint(request):
            raise ServiceError("IDEMPOTENCY_CONFLICT", "同一幂等键的请求内容不同", 409)
        return record.response_body
    return None


def save_replay(db: Session, operation: str, scope: str, key: str | None, request: dict, task: Task, status: int):
    if key:
        db.add(IdempotencyRecord(operation=operation, scope_id=scope, key=key, task_id=task.id,
                                 request_hash=fingerprint(request), response_status=status,
                                 response_body=task_json(task), expires_at=utcnow() + timedelta(hours=24)))


def create_task(db: Session, query: str, max_sources: int, key: str | None = None) -> dict:
    request = {"query": query, "max_sources": max_sources}
    existing = replay(db, "create", "create", key, request)
    if existing:
        return existing
    if db.scalar(select(func.count(Task.id))) >= settings.max_tasks:
        raise ServiceError("CAPACITY_EXCEEDED", "任务容量已满", 429)
    now = utcnow()
    task = Task(id=uid(), query=query, status="queued", options={"max_sources": max_sources},
                attempt=1, cancel_requested=False, created_at=now, updated_at=now)
    db.add(task)
    db.flush()
    event(db, task, "task.queued", {"status": "queued"})
    save_replay(db, "create", "create", key, request, task, 201)
    db.commit()
    return task_json(task)


def get_task(db: Session, task_id: str, lock: bool = False) -> Task:
    stmt = select(Task).where(Task.id == task_id)
    if lock:
        stmt = stmt.with_for_update()
    task = db.scalar(stmt)
    if not task:
        raise ServiceError("NOT_FOUND", "任务不存在", 404)
    return task


def cancel_task(db: Session, task_id: str) -> dict:
    t = get_task(db, task_id, lock=True)
    if t.status == "cancelled" or (t.status == "running" and t.cancel_requested):
        return task_json(t)
    if t.status not in ("queued", "running"):
        raise ServiceError("INVALID_STATE", "当前状态不能取消", 409)
    if t.status == "queued":
        t.status = "cancelled"
        event(db, t, "task.cancelled", {"status": "cancelled"})
    else:
        t.cancel_requested = True
        event(db, t, "task.cancel_requested", {"status": "running", "cancel_requested": True})
    t.version += 1
    t.updated_at = utcnow()
    db.commit()
    return task_json(t)


def resume_task(db: Session, task_id: str, key: str | None = None) -> dict:
    existing = replay(db, "resume", task_id, key, {})
    if existing:
        return existing
    t = get_task(db, task_id, lock=True)
    if t.status not in ("failed", "cancelled"):
        raise ServiceError("INVALID_STATE", "当前状态不能恢复", 409)
    if t.attempt >= 3:
        raise ServiceError("ATTEMPT_LIMIT_REACHED", "恢复次数已用完", 409)
    if t.config_fingerprint != "v1" or t.schema_version != 1:
        raise ServiceError("RESUME_INCOMPATIBLE", "任务配置已不兼容", 409)
    t.status, t.cancel_requested = "queued", False
    t.attempt += 1
    t.lease_owner = t.lease_token = t.lease_expires_at = None
    t.error_code = t.error_message = None
    t.version += 1
    t.updated_at = utcnow()
    event(db, t, "task.queued", {"status": "queued"})
    save_replay(db, "resume", task_id, key, {}, t, 202)
    db.commit()
    return task_json(t)
