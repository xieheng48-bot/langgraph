import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .graph import TicketTool, build_graph
from .llm import Advisor
from .models import ApprovalRequest, DetailRequest, Principal, ReportRequest
from .repository import Repository


TOKENS = {
    "demo-alice": Principal("alice", "applicant"),
    "demo-bob": Principal("bob", "applicant"),
    "demo-it": Principal("it", "reviewer"),
}


def principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
    if not authorization or not authorization.startswith("Bearer ") or authorization[7:] not in TOKENS:
        raise HTTPException(401, "请使用演示身份 Bearer demo-alice、demo-bob 或 demo-it")
    return TOKENS[authorization[7:]]


User = Annotated[Principal, Depends(principal)]


class Runtime:
    def __init__(self, db_path: str, ticket_tool: TicketTool | None, model_mode: str | None, llm_client: httpx.Client | None):
        self.lock = RLock()
        self.advisor = Advisor(model_mode, llm_client)
        try:
            self.repo = Repository(db_path)
            self.checkpoint_connection = sqlite3.connect(db_path, check_same_thread=False)
            self.checkpoint_connection.execute("PRAGMA busy_timeout=5000")
            self.saver = SqliteSaver(self.checkpoint_connection)
            self.saver.setup()
            self.graph = build_graph(self.repo, self.saver, self.advisor, ticket_tool)
        except Exception:
            self.advisor.close()
            if hasattr(self, "checkpoint_connection"):
                self.checkpoint_connection.close()
            if hasattr(self, "repo"):
                self.repo.close()
            raise

    @staticmethod
    def config(run_id: str) -> dict:
        return {"configurable": {"thread_id": "it:" + run_id}}

    def close(self) -> None:
        self.advisor.close()
        self.checkpoint_connection.close()
        self.repo.close()

    def owned_run(self, run_id: str, user: Principal) -> dict:
        run = self.repo.run(run_id)
        if not run:
            raise HTTPException(404, "任务不存在")
        if user.role != "reviewer" and run["owner"] != user.user_id:
            raise HTTPException(403, "只能访问自己的报障任务")
        return run

    def check_device(self, device_id: str | None, owner: str) -> None:
        if not device_id:
            return
        device = self.repo.device(device_id)
        if not device:
            raise HTTPException(404, "设备不存在")
        if device["owner"] != owner:
            raise HTTPException(403, "不能为他人的设备提交报障")

    def view(self, run_id: str) -> dict:
        run = self.repo.run(run_id)
        snapshot = self.graph.get_state(self.config(run_id))
        pending = [item.value for item in snapshot.interrupts]
        unstarted = run["status"] == "collecting" and self.saver.get_tuple(self.config(run_id)) is None
        return {**run, "run_id": run_id, "model_mode": snapshot.values.get("model_mode", self.advisor.mode), "sources": snapshot.values.get("sources", []), "pending": pending, "next_nodes": list(snapshot.next), "can_resume": not pending and bool(snapshot.next or unstarted), "ticket": self.repo.ticket(run_id), "audit": self.repo.audit(run_id)}


def create_app(db_path: str | Path | None = None, *, ticket_tool: TicketTool | None = None, model_mode: str | None = None, llm_client: httpx.Client | None = None) -> FastAPI:
    selected_path = str(db_path or os.getenv("IT_DB_PATH", "data/it-service.db"))
    if selected_path == ":memory:":
        raise ValueError("请传入文件路径，以便业务连接与持久化检查点共享数据库")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = Runtime(selected_path, ticket_tool, model_mode, llm_client)
        app.state.runtime = runtime
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title="企业 IT 服务台 Agent · 教学演示", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return Path(__file__).with_name("index.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health():
        return {"status": "ok", "model_mode": app.state.runtime.advisor.mode}

    @app.get("/api/devices")
    def devices(user: User):
        runtime = app.state.runtime
        with runtime.lock:
            return runtime.repo.devices(None if user.role == "reviewer" else user.user_id)

    @app.get("/api/runs")
    def runs(user: User):
        runtime = app.state.runtime
        with runtime.lock:
            return runtime.repo.runs(None if user.role == "reviewer" else user.user_id)

    @app.post("/api/runs", status_code=201)
    def report(body: ReportRequest, user: User):
        if user.role != "applicant":
            raise HTTPException(403, "请切换为员工身份报障")
        runtime = app.state.runtime
        with runtime.lock:
            runtime.check_device(body.device_id, user.user_id)
            run_id = runtime.repo.create_run(user.user_id, body.description.strip(), body.device_id)
            runtime.graph.invoke({"run_id": run_id, "owner": user.user_id, "description": body.description.strip(), "device_id": body.device_id, "status": "collecting"}, runtime.config(run_id))
            return runtime.view(run_id)

    @app.get("/api/runs/{run_id}")
    def inspect(run_id: str, user: User):
        runtime = app.state.runtime
        with runtime.lock:
            runtime.owned_run(run_id, user)
            return runtime.view(run_id)

    @app.post("/api/runs/{run_id}/details")
    def details(run_id: str, body: DetailRequest, user: User):
        runtime = app.state.runtime
        with runtime.lock:
            run = runtime.owned_run(run_id, user)
            pending = runtime.view(run_id)["pending"]
            if not pending or pending[0].get("type") != "missing_fields":
                raise HTTPException(409, "任务当前没有等待补充资料")
            description = body.description.strip() if body.description is not None else run["description"]
            device_id = body.device_id or run["device_id"]
            if len(description) < 4 or not device_id:
                raise HTTPException(422, "请填写至少四个字符的问题描述和设备编号")
            runtime.check_device(device_id, run["owner"])
            runtime.graph.invoke(Command(resume={"description": description, "device_id": device_id}), runtime.config(run_id))
            return runtime.view(run_id)

    @app.post("/api/runs/{run_id}/approval")
    def approve(run_id: str, body: ApprovalRequest, user: User):
        runtime = app.state.runtime
        with runtime.lock:
            run = runtime.owned_run(run_id, user)
            if user.role != "reviewer":
                raise HTTPException(403, "只有 IT 审批人可以批准或拒绝")
            if run["version"] != body.version or run["digest"] != body.digest:
                raise HTTPException(409, "提案版本或摘要不匹配，请刷新后重新审批")
            if run["decision"] and run["decision"] != body.decision:
                raise HTTPException(409, "任务已作出相反的审批决定")
            view = runtime.view(run_id)
            pending = view["pending"]
            if not pending or pending[0].get("type") != "approval":
                if run["decision"] == body.decision:
                    if view["next_nodes"]:
                        runtime.graph.invoke(None, runtime.config(run_id))
                        view = runtime.view(run_id)
                    return {**view, "replayed": True}
                raise HTTPException(409, "任务当前没有等待审批")
            runtime.graph.invoke(Command(resume={**body.model_dump(), "reviewer": user.user_id}), runtime.config(run_id))
            return runtime.view(run_id)

    @app.post("/api/runs/{run_id}/retry")
    def retry(run_id: str, user: User):
        runtime = app.state.runtime
        with runtime.lock:
            run = runtime.owned_run(run_id, user)
            if run["status"] == "tool_failed":
                if user.role != "reviewer":
                    raise HTTPException(403, "工单执行失败须由 IT 审批人核查后重试")
                runtime.graph.update_state(runtime.config(run_id), {"status": "approved", "error": None}, as_node="await_approval")
            elif run["status"] == "model_failed":
                runtime.graph.update_state(runtime.config(run_id), {"status": "proposing", "error": None}, as_node="retrieve")
            else:
                raise HTTPException(409, "只有模型建议或工单工具失败时可重试")
            runtime.graph.invoke(None, runtime.config(run_id))
            return runtime.view(run_id)

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str, user: User):
        runtime = app.state.runtime
        with runtime.lock:
            run = runtime.owned_run(run_id, user)
            view = runtime.view(run_id)
            if view["pending"]:
                raise HTTPException(409, "正在等待补充资料或人工审批，请使用对应操作")
            if not view["next_nodes"]:
                if run["status"] == "collecting" and runtime.saver.get_tuple(runtime.config(run_id)) is None:
                    # 业务任务已提交、图的首个检查点尚未落盘时进程退出。
                    runtime.graph.invoke({"run_id": run_id, "owner": run["owner"], "description": run["description"], "device_id": run["device_id"], "status": "collecting"}, runtime.config(run_id))
                    return runtime.view(run_id)
                raise HTTPException(409, "没有需要恢复的未完成节点")
            if run["decision"] == "approve" and user.role != "reviewer":
                raise HTTPException(403, "已审批的工单执行流程只能由 IT 人员恢复")
            runtime.graph.invoke(None, runtime.config(run_id))
            return runtime.view(run_id)

    return app


app = create_app()
