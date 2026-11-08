import asyncio
import json
import re
import uuid

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import settings
from .db import SessionLocal
from .models import Task, TaskEvent, TaskStep
from .service import ServiceError, cancel_task, create_task, get_task, resume_task, task_json


app = FastAPI(title="WebPilot API", version="0.1.0")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def local_write_guard(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin not in {"http://127.0.0.1:8000", "http://localhost:8000"}:
            return db_error(ServiceError("ACCESS_DENIED", "请求来源不受信任", 403))
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return db_error(ServiceError("UNSUPPORTED_MEDIA_TYPE", "请使用 JSON 请求", 415))
    return await call_next(request)


def db_session():
    with SessionLocal() as db:
        yield db


def db_error(exc: ServiceError):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message,
                                   "details": {}, "request_id": str(uuid.uuid4())}}, status_code=exc.status)


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, exc: ServiceError):
    return db_error(exc)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError):
    return db_error(ServiceError("VALIDATION_ERROR", "请求参数不合法", 422))


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_sources: int = Field(default=10, ge=1, le=20)


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    options: Options = Field(default_factory=Options)

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str):
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value


def idempotency_key(value: str | None = Header(default=None, alias="Idempotency-Key")):
    if value is not None and not re.fullmatch(r"[!-~]{1,128}", value):
        raise ServiceError("VALIDATION_ERROR", "幂等键格式错误", 422)
    return value


@app.post("/api/v1/tasks", status_code=201)
def create(body: CreateTask, db: Session = Depends(db_session), key: str | None = Depends(idempotency_key)):
    return create_task(db, body.query, body.options.max_sources, key)


@app.get("/api/v1/tasks")
def list_tasks(status: str | None = None, limit: int = Query(20, ge=1, le=100),
               offset: int = Query(0, ge=0), db: Session = Depends(db_session)):
    if status is not None and status not in {"queued", "running", "completed", "failed", "cancelled"}:
        raise ServiceError("VALIDATION_ERROR", "未知任务状态", 422)
    stmt = select(Task)
    if status:
        stmt = stmt.where(Task.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [task_json(t) for t in rows], "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/tasks/{task_id}")
def detail(task_id: str, db: Session = Depends(db_session)):
    return task_json(get_task(db, task_id))


@app.post("/api/v1/tasks/{task_id}/cancel", status_code=202)
def cancel(task_id: str, db: Session = Depends(db_session)):
    return cancel_task(db, task_id)


@app.post("/api/v1/tasks/{task_id}/resume", status_code=202)
def resume(task_id: str, db: Session = Depends(db_session), key: str | None = Depends(idempotency_key)):
    return resume_task(db, task_id, key)


def paged(db: Session, model, task_id: str, limit: int, offset: int):
    get_task(db, task_id)
    stmt = select(model).where(model.task_id == task_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return rows, total


@app.get("/api/v1/tasks/{task_id}/steps")
def steps(task_id: str, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
          db: Session = Depends(db_session)):
    rows, total = paged(db, TaskStep, task_id, limit, offset)
    return {"items": [{"id": s.id, "step_key": s.step_key, "attempt": s.attempt,
                        "type": s.type, "status": s.status, "input": s.input, "output": s.output,
                        "started_at": s.started_at, "finished_at": s.finished_at} for s in rows],
            "total": total, "limit": limit, "offset": offset}


def read_events(db: Session, task_id: str, after: int, limit: int):
    get_task(db, task_id)
    maximum = db.scalar(select(func.max(TaskEvent.sequence)).where(TaskEvent.task_id == task_id)) or 0
    if after > maximum:
        raise ServiceError("VALIDATION_ERROR", "事件游标超出范围", 422)
    rows = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.sequence > after)
                      .order_by(TaskEvent.sequence).limit(limit)).all()
    return [{"sequence": e.sequence, "attempt": e.attempt, "type": e.type,
             "payload": e.payload, "created_at": e.created_at.isoformat() + ("Z" if e.created_at.tzinfo is None else "")} for e in rows]


@app.get("/api/v1/tasks/{task_id}/events")
def events(task_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100),
           db: Session = Depends(db_session)):
    items = read_events(db, task_id, after, limit)
    return {"items": items, "next_after": items[-1]["sequence"] if items else after}


@app.get("/api/v1/tasks/{task_id}/events/stream")
async def stream(task_id: str, request: Request, after: int = Query(0, ge=0)):
    header = request.headers.get("Last-Event-ID")
    if header is not None:
        if not header.isdigit():
            raise ServiceError("VALIDATION_ERROR", "事件游标格式错误", 422)
        after = int(header)
    with SessionLocal() as db:
        read_events(db, task_id, after, 1)
        t = get_task(db, task_id)
        maximum = db.scalar(select(func.max(TaskEvent.sequence)).where(TaskEvent.task_id == task_id)) or 0
        if t.status in {"completed", "failed", "cancelled"} and maximum == after:
            return JSONResponse(status_code=204, content=None)

    async def generate():
        cursor = after
        tick = 0
        while not await request.is_disconnected():
            with SessionLocal() as db:
                items = read_events(db, task_id, cursor, 100)
                t = get_task(db, task_id)
            for item in items:
                cursor = item["sequence"]
                yield f'id: {cursor}\nevent: {item["type"]}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n'
            if t.status in {"completed", "failed", "cancelled"} and t.attempt == (items[-1]["attempt"] if items else t.attempt):
                return
            if tick % 15 == 0:
                yield ": heartbeat\n\n"
            tick += 1
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/ready")
def ready(db: Session = Depends(db_session)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise ServiceError("CONFIGURATION_ERROR", "数据库不可用", 503)
    return {"status": "ready"}


@app.get("/api/v1/capabilities")
def capabilities():
    return {"browser": {"enabled": False, "reason": "浏览器隔离尚未完成验证"},
            "max_sources": 20, "max_attempts": 3}
