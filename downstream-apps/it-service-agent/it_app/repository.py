import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    """业务数据；检查点由 LangGraph SqliteSaver 单独管理。"""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sops (
                id TEXT PRIMARY KEY, category TEXT NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL, version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, description TEXT NOT NULL,
                device_id TEXT, status TEXT NOT NULL, proposal TEXT,
                version INTEGER NOT NULL DEFAULT 0, digest TEXT,
                decision TEXT, approved_by TEXT, reason TEXT,
                error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
                owner TEXT NOT NULL, device_id TEXT NOT NULL REFERENCES devices(id),
                title TEXT NOT NULL, category TEXT NOT NULL,
                proposal_version INTEGER NOT NULL, proposal_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                event TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        with self.conn:
            self.conn.executemany("INSERT OR IGNORE INTO devices VALUES (?, ?, ?)", [
                ("LAPTOP-A01", "alice", "Alice 的演示笔记本"),
                ("LAPTOP-B01", "bob", "Bob 的演示笔记本"),
            ])
            self.conn.executemany("INSERT OR IGNORE INTO sops VALUES (?, ?, ?, ?, ?)", [
                ("SOP-NET-001", "network", "办公网络连接排查", "检查 Wi-Fi 开关及公司网络名称；记录错误提示；重连网络后仍失败，提交 IT 工单。不要向他人提供网络密码。", 1),
                ("SOP-ACCOUNT-001", "account", "账号登录异常", "确认账号与错误提示；仅通过公司官方自助页面重置密码；禁止向助手提交密码或验证码；仍无法登录时转 IT。", 1),
                ("SOP-DISK-001", "storage", "磁盘空间不足", "查看可用空间并记录；清理个人确认无用的临时文件前先备份；不要自动删除业务资料；必要时提交扩容工单。", 1),
                ("SOP-GEN-001", "general", "通用 IT 报障", "收集设备编号、问题现象和发生时间；不要执行未知命令；通过 IT 工单安排人工排查。", 1),
            ])

    def close(self) -> None:
        self.conn.close()

    def device(self, device_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return dict(row) if row else None

    def devices(self, owner: str | None) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM devices WHERE owner = ? ORDER BY id", (owner,)) if owner else self.conn.execute("SELECT * FROM devices ORDER BY id")
        return [dict(row) for row in rows]

    def create_run(self, owner: str, description: str, device_id: str | None) -> str:
        run_id = uuid.uuid4().hex
        timestamp = now()
        with self.conn:
            self.conn.execute("INSERT INTO runs (id, owner, description, device_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'collecting', ?, ?)", (run_id, owner, description, device_id, timestamp, timestamp))
            self._audit(run_id, "reported", owner, {})
        return run_id

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["proposal"] = json.loads(item["proposal"]) if item["proposal"] else None
        return item

    def runs(self, owner: str | None) -> list[dict]:
        rows = self.conn.execute("SELECT id FROM runs WHERE owner = ? ORDER BY created_at DESC", (owner,)) if owner else self.conn.execute("SELECT id FROM runs ORDER BY created_at DESC")
        return [self.run(row["id"]) for row in rows]

    def save_details(self, run_id: str, description: str, device_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET description = ?, device_id = ?, error = NULL, updated_at = ? WHERE id = ?", (description, device_id, now(), run_id))

    def sops(self, category: str) -> list[dict]:
        # 固定分类 + 关键词选择，不是向量数据库，也没有 embedding。
        rows = self.conn.execute("SELECT * FROM sops WHERE category IN (?, 'general') ORDER BY CASE WHEN category = ? THEN 0 ELSE 1 END, id", (category, category))
        return [dict(row) for row in rows]

    def save_proposal(self, run_id: str, proposal: dict, digest: str) -> int:
        with self.conn:
            self.conn.execute("UPDATE runs SET proposal = ?, digest = ?, version = version + 1, status = 'waiting_approval', error = NULL, updated_at = ? WHERE id = ?", (json.dumps(proposal, ensure_ascii=False), digest, now(), run_id))
            version = self.run(run_id)["version"]
            self._audit(run_id, "proposal_created", "system", {"version": version, "digest": digest})
        return version

    def decide(self, run_id: str, decision: str, version: int, digest: str, reviewer: str, reason: str) -> None:
        if reviewer != "it":
            raise ValueError("审批人没有 IT 审批权限")
        with self.conn:
            previous = self.run(run_id)
            if previous and previous["decision"] == decision and previous["version"] == version and previous["digest"] == digest and previous["approved_by"] == reviewer:
                return
            cursor = self.conn.execute("UPDATE runs SET decision = ?, approved_by = ?, reason = ?, status = ?, updated_at = ? WHERE id = ? AND version = ? AND digest = ? AND status = 'waiting_approval'", (decision, reviewer, reason, "approved" if decision == "approve" else "rejected", now(), run_id, version, digest))
            if cursor.rowcount != 1:
                raise ValueError("审批已失效或已被处理")
            self._audit(run_id, "approved" if decision == "approve" else "rejected", reviewer, {"version": version, "digest": digest, "reason": reason})

    def status(self, run_id: str, status: str, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE id = ?", (status, error, now(), run_id))

    def ticket(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM tickets WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def create_ticket(self, run_id: str, version: int, digest: str) -> str:
        """同一事务内校验审批并写工单；run_id 唯一约束承担最终幂等。"""
        with self.conn:
            run = self.run(run_id)
            if not run or run["decision"] != "approve" or run["approved_by"] != "it" or run["version"] != version or run["digest"] != digest:
                raise ValueError("工单未获得当前版本的有效审批")
            existing = self.ticket(run_id)
            if existing:
                return existing["id"]
            device = self.device(run["device_id"])
            if not device or device["owner"] != run["owner"]:
                raise ValueError("设备归属校验失败")
            proposal = run["proposal"]
            ticket_id = "IT-" + uuid.uuid4().hex[:12].upper()
            self.conn.execute("INSERT INTO tickets (id, run_id, owner, device_id, title, category, proposal_version, proposal_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (ticket_id, run_id, run["owner"], run["device_id"], proposal["title"], proposal["category"], version, digest, now()))
            self._audit(run_id, "ticket_created", "it", {"ticket_id": ticket_id})
        return ticket_id

    def audit(self, run_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT event, actor, detail, created_at FROM audit WHERE run_id = ? ORDER BY id", (run_id,))
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def _audit(self, run_id: str, event: str, actor: str, detail: dict) -> None:
        self.conn.execute("INSERT INTO audit (run_id, event, actor, detail, created_at) VALUES (?, ?, ?, ?, ?)", (run_id, event, actor, json.dumps(detail, ensure_ascii=False), now()))
