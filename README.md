# WebPilot Agent

开源技术选型调研助手。当前仓库处于逐模块开发阶段；已提交设计文档和任务基础模块，调研工作流尚未接入。完整需求和接口设计见 [docs/README.md](docs/README.md)。

## 本地开发

需要 Python 3.11+、PostgreSQL。环境变量 `WEBPILOT_DATABASE_URL` 默认指向本机的 `webpilot` 数据库。不要提交 `.env` 或 API Key。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m webpilot.init_db
.\.venv\Scripts\python.exe -m uvicorn webpilot.main:app --host 127.0.0.1 --port 8000
.\.venv\Scripts\python.exe -m pytest
```

`init_db` 应用初始数据库版本；后续结构变化需新增版本迁移。当前 API 可以创建、查询、取消和恢复任务，但不会自动执行调研。DeepSeek 与 Edge 的适配尚未接入。
