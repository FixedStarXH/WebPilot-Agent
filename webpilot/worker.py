"""Lease and state transition helpers for a single worker."""
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Task, TaskStep, uid, utcnow
from .service import ServiceError, event, get_task


def claim(db: Session, owner: str) -> tuple[str, str] | None:
    """Claim one queued task; PostgreSQL SKIP LOCKED prevents double claims."""
    task = db.scalar(select(Task).where(Task.status == "queued")
                     .order_by(Task.created_at, Task.id).with_for_update(skip_locked=True).limit(1))
    if task is None:
        db.rollback()
        return None
    task.status, task.lease_owner, task.lease_token = "running", owner, uid()
    task.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
    task.version += 1
    task.updated_at = utcnow()
    event(db, task, "task.started", {"status": "running"})
    result = task.id, task.lease_token
    db.commit()
    return result


def locked_task(db: Session, task_id: str, token: str) -> Task:
    task = get_task(db, task_id, lock=True)
    expiry = task.lease_expires_at
    if task.status != "running" or task.lease_token != token or not expiry or expiry.replace(tzinfo=expiry.tzinfo or timezone.utc) <= utcnow():
        raise ServiceError("LEASE_LOST", "任务租约已失效", 409)
    return task


def heartbeat(db: Session, task_id: str, token: str) -> bool:
    task = locked_task(db, task_id, token)
    if task.cancel_requested:
        stop(db, task_id, token, "cancelled")
        return False
    task.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
    task.updated_at = utcnow()
    db.commit()
    return True


def stop(db: Session, task_id: str, token: str, outcome: str, code: str | None = None, message: str | None = None):
    task = locked_task(db, task_id, token)
    if task.cancel_requested:
        outcome = "cancelled"
    if outcome not in ("failed", "cancelled"):
        raise ValueError("outcome must be failed or cancelled")
    task.status = outcome
    task.error_code = code if outcome == "failed" else None
    task.error_message = message if outcome == "failed" else None
    task.lease_owner = task.lease_token = task.lease_expires_at = None
    task.version += 1
    task.updated_at = utcnow()
    unfinished = db.scalars(select(TaskStep).where(TaskStep.task_id == task_id,
                         TaskStep.attempt == task.attempt, TaskStep.status == "running")).all()
    for step in unfinished:
        step.status = "cancelled" if outcome == "cancelled" else "failed"
        step.finished_at = utcnow()
        event(db, task, f"step.{step.status}", {"step_id": step.id, "step_key": step.step_key,
              "type": step.type, **({"error": {"code": code or "WORKER_INTERRUPTED", "message": message or "任务中断"}} if outcome == "failed" else {})})
    event(db, task, f"task.{outcome}", {"status": outcome,
          **({"error": {"code": code or "INTERNAL_ERROR", "message": message or "任务失败"}} if outcome == "failed" else {})})
    db.commit()


def recover_expired(db: Session):
    """Mark abandoned tasks terminal, making them eligible for manual resume."""
    now = utcnow()
    rows = db.scalars(select(Task).where(Task.status == "running", Task.lease_expires_at < now)
                      .with_for_update(skip_locked=True)).all()
    for task in rows:
        outcome = "cancelled" if task.cancel_requested else "failed"
        task.status = outcome
        task.error_code = None if outcome == "cancelled" else "WORKER_INTERRUPTED"
        task.error_message = None if outcome == "cancelled" else "worker 租约过期"
        task.lease_owner = task.lease_token = task.lease_expires_at = None
        task.version += 1
        task.updated_at = now
        for step in db.scalars(select(TaskStep).where(TaskStep.task_id == task.id,
                               TaskStep.attempt == task.attempt, TaskStep.status == "running")).all():
            step.status = "cancelled" if outcome == "cancelled" else "failed"
            step.finished_at = now
            event(db, task, f"step.{step.status}", {"step_id": step.id, "step_key": step.step_key,
                  "type": step.type, **({"error": {"code": "WORKER_INTERRUPTED", "message": "worker 租约过期"}} if outcome == "failed" else {})})
        event(db, task, f"task.{outcome}", {"status": outcome,
              **({"error": {"code": "WORKER_INTERRUPTED", "message": "worker 租约过期"}} if outcome == "failed" else {})})
    db.commit()
    return len(rows)
