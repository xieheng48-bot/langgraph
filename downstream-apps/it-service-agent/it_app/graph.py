import hashlib
import json
from typing import Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .llm import Advisor, ModelError
from .repository import Repository


class ServiceState(TypedDict, total=False):
    run_id: str
    owner: str
    description: str
    device_id: str | None
    category: str
    sources: list[dict]
    proposal: dict
    version: int
    digest: str
    status: str
    model_mode: str
    ticket_id: str
    error: str | None


TicketTool = Callable[[Repository, ServiceState], str]


def classify(description: str) -> str:
    value = description.lower()
    for category, words in [
        ("network", ["网络", "wifi", "wi-fi", "断网", "连接"]),
        ("account", ["账号", "登录", "密码", "验证码"]),
        ("storage", ["磁盘", "空间", "硬盘", "存储"]),
    ]:
        if any(word in value for word in words):
            return category
    return "general"


def default_ticket_tool(repo: Repository, state: ServiceState) -> str:
    return repo.create_ticket(state["run_id"], state["version"], state["digest"])


def build_graph(repo: Repository, saver: SqliteSaver, advisor: Advisor, ticket_tool: TicketTool | None = None):
    tool = ticket_tool or default_ticket_tool

    def collect(state: ServiceState) -> dict:
        description = state.get("description", "").strip()
        device_id = state.get("device_id")
        missing = []
        if len(description) < 4:
            missing.append("description")
        if not device_id:
            missing.append("device_id")
        if missing:
            answer = interrupt({"type": "missing_fields", "required": missing, "message": "请补充至少四个字符的问题描述及本人设备编号。"})
            description = answer["description"].strip()
            device_id = answer["device_id"]
        # 节点仍执行一次服务端防线；不能靠模型或网页校验设备归属。
        device = repo.device(device_id)
        if len(description) < 4 or not device or device["owner"] != state["owner"]:
            raise ValueError("问题描述或设备归属不合法")
        repo.save_details(state["run_id"], description, device_id)
        return {"description": description, "device_id": device_id, "category": classify(description), "status": "retrieving"}

    def retrieve(state: ServiceState) -> dict:
        return {"sources": repo.sops(state["category"]), "status": "proposing"}

    def propose(state: ServiceState) -> dict:
        previous = repo.run(state["run_id"])
        if previous["proposal"] and previous["status"] == "waiting_approval":
            # 节点执行后、检查点写入前中断时，复用已保存提案及版本。
            return {"proposal": previous["proposal"], "version": previous["version"], "digest": previous["digest"], "status": "waiting_approval", "model_mode": previous["proposal"]["model_mode"], "error": None}
        try:
            suggestion = advisor.suggest(state["description"], state["sources"])
        except ModelError as exc:
            repo.status(state["run_id"], "model_failed", str(exc))
            return {"status": "model_failed", "model_mode": advisor.mode, "error": str(exc)}
        proposal = {
            "action": "create_ticket",
            "owner": state["owner"],
            "device_id": state["device_id"],
            "category": state["category"],
            "title": state["description"][:100],
            "description": state["description"],
            "advice": suggestion["advice"],
            "citation_ids": suggestion["citation_ids"],
            "source_versions": {item["id"]: item["version"] for item in state["sources"]},
            "model_mode": advisor.mode,
        }
        digest = hashlib.sha256(json.dumps(proposal, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        version = repo.save_proposal(state["run_id"], proposal, digest)
        return {"proposal": proposal, "version": version, "digest": digest, "status": "waiting_approval", "model_mode": advisor.mode, "error": None}

    def await_approval(state: ServiceState) -> dict:
        answer = interrupt({"type": "approval", "message": "请 IT 审批人核对提案后批准或拒绝。批准只创建工单，不执行修复命令。", "version": state["version"], "digest": state["digest"], "proposal": state["proposal"]})
        if answer["version"] != state["version"] or answer["digest"] != state["digest"]:
            raise ValueError("审批提案版本已失效")
        repo.decide(state["run_id"], answer["decision"], answer["version"], answer["digest"], answer["reviewer"], answer.get("reason", ""))
        return {"status": "approved" if answer["decision"] == "approve" else "rejected"}

    def create_ticket(state: ServiceState) -> dict:
        try:
            existing = repo.ticket(state["run_id"])
            ticket_id = existing["id"] if existing else tool(repo, state)
            return {"ticket_id": ticket_id, "status": "verifying", "error": None}
        except Exception:
            message = "工单工具执行失败，审批已保留；请由 IT 审批人核查后重试。"
            repo.status(state["run_id"], "tool_failed", message)
            return {"status": "tool_failed", "error": message}

    def verify(state: ServiceState) -> dict:
        ticket = repo.ticket(state["run_id"])
        valid = ticket and ticket["id"] == state.get("ticket_id") and ticket["owner"] == state["owner"] and ticket["device_id"] == state["device_id"] and ticket["proposal_version"] == state["version"] and ticket["proposal_digest"] == state["digest"]
        if not valid:
            message = "工单结果核验失败，请 IT 人员检查本地工具。"
            repo.status(state["run_id"], "tool_failed", message)
            return {"status": "tool_failed", "error": message}
        repo.status(state["run_id"], "completed")
        return {"status": "completed", "error": None}

    workflow = StateGraph(ServiceState)
    for name, node in [("collect", collect), ("retrieve", retrieve), ("propose", propose), ("await_approval", await_approval), ("create_ticket", create_ticket), ("verify", verify)]:
        workflow.add_node(name, node)
    workflow.add_edge(START, "collect")
    workflow.add_edge("collect", "retrieve")
    workflow.add_edge("retrieve", "propose")
    workflow.add_conditional_edges("propose", lambda state: "stop" if state["status"] == "model_failed" else "approval", {"stop": END, "approval": "await_approval"})
    workflow.add_conditional_edges("await_approval", lambda state: "create" if state["status"] == "approved" else "stop", {"create": "create_ticket", "stop": END})
    workflow.add_conditional_edges("create_ticket", lambda state: "stop" if state["status"] == "tool_failed" else "verify", {"stop": END, "verify": "verify"})
    workflow.add_edge("verify", END)
    return workflow.compile(checkpointer=saver)
