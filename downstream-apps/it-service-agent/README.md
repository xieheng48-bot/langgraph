# 企业 IT 服务台 Agent · 下游业务应用

一个可运行、可读懂的求职作品：员工提交设备报障，系统收集必要资料、检索 SOP、生成建议，IT 人员审批后创建本地工单，并核验结果。人工审批及缺资料补问均使用真实 LangGraph `interrupt()`；恢复使用 `Command(resume=...)`，暂停点通过 `SqliteSaver` 保存到磁盘。

本目录是由谢恒（[GitHub: xieheng48-bot](https://github.com/xieheng48-bot)）维护、经 AI 辅助实现与自动化测试的下游业务应用。应用自己定义报障、审批与工单规则，使用 LangGraph 提供的执行、检查点和中断/恢复能力；没有新增 LangGraph 核心 Agent SDK、harness 或运行时 API。来源和协作说明见 [AUTHORS.md](AUTHORS.md)，fork 范围见仓库根目录 [DOWNSTREAM.md](../../DOWNSTREAM.md)。

技术实现是 FastAPI 后端与 HTML 演示页，没有克隆 `full-stack-fastapi-template`，没有 React 管理台，也没有接入真实企业 IT 系统。可以作为个人实战作品介绍，不能写成已上线的公司项目，也不声称已经过人工验收。

## 安装与启动

需要 Python 3.10+ 和 uv。从 fork 仓库根目录进入此应用，再安装依赖：

```powershell
cd downstream-apps/it-service-agent
uv sync --group dev
uv run uvicorn it_app.main:app --host 127.0.0.1 --port 8012
```

打开 <http://127.0.0.1:8012>；交互式 API 文档位于 <http://127.0.0.1:8012/docs>。

应用通过 `pyproject.toml` 安装发布版 LangGraph 及 SQLite checkpointer；没有直接修改或导入仓库 `libs/` 中的上游源码。应用测试验证本目录的业务行为，不代表上游运行时全套测试已经通过。

只运行一个进程/worker。不要添加 `--workers 2`。默认数据库为项目工作目录下的 `data/it-service.db`，启动时自动建立演示数据。没有清空数据库的网页按钮。

## 五分钟验收

1. 以“员工 Alice”提交默认网络问题，设备选择 `LAPTOP-A01`。任务暂停在“等待 IT 审批”。查看来源 `SOP-NET-001` 及版本/摘要。
2. 切换“员工 Bob”，Alice 的任务不出现在列表里。后端同样拒绝跨用户查看或给他人的设备报障。
3. 切换“IT 审批人”，选择 Alice 的任务，核对提案后批准。页面展示真实写入 SQLite 的 `IT-...` 工单。
4. 再提交一个任务并拒绝，确认没有生成工单。重复相同审批保持幂等；相反决定返回冲突。
5. 再提交任务但选择“暂未提供”设备，演示缺资料补问。待审批时关闭服务器并用同一命令重启，任务及审批暂停点仍存在。

这些静态演示口令是方便本地学习的身份模拟，不是生产认证方案：

| Bearer token | 身份 | 权限 |
|---|---|---|
| `demo-alice` | 员工 Alice | 本人的设备/任务、补充资料 |
| `demo-bob` | 员工 Bob | 本人的设备/任务、补充资料 |
| `demo-it` | IT 审批人 | 查看所有演示任务、审批、工单失败重试 |

员工不能自己审批。身份来自服务端固定 token 映射，忽略请求体或模型声称的 `owner` / `reviewer`。

## 默认规则演示与可选大模型

默认 `MODEL_MODE=offline`，不调用模型、不产生模型费用：问题按关键词分类，再通过 SQL 取出对应分类及通用 SOP，建议直接取 SOP 文本。**没有 embedding、向量数据库、混合检索或语义重排**；这是可验证的规则基线。

若已有支持 Chat Completions 兼容协议的模型服务，可在启动终端显式设置：

```powershell
$env:MODEL_MODE = "llm"
$env:LLM_BASE_URL = "https://你的模型服务/v1"
$env:LLM_API_KEY = "你的密钥"
$env:LLM_MODEL = "服务支持的模型名称"
uv run uvicorn it_app.main:app --host 127.0.0.1 --port 8012
```

`.env.example` 只是配置说明；程序不会自动读取 `.env`。LLM 模式会将报障描述和检索到的 SOP 发送给配置的服务；演示时只使用虚构资料。模型返回的建议和来源 ID 会校验格式及来源存在性。模型不决定设备身份、权限、工单参数或审批结果。模型调用失败会停在 `model_failed`，页面明确提示并允许重试，不会伪装为模型成功或直接创建工单。来源 ID 有效不等于建议语义一定正确，IT 人员仍需审核。

## 路由

除首页和健康检查外，均需 `Authorization: Bearer <演示 token>`。

| 方法/路径 | 作用 |
|---|---|
| `GET /health` | 健康状态及运行模式 |
| `GET /api/devices` | 当前员工设备；IT 可查看全部 |
| `GET /api/runs` | 当前可见任务 |
| `POST /api/runs` | `{description, device_id}` 创建报障并运行到暂停点 |
| `GET /api/runs/{id}` | 提案、来源、暂停点、工单及审计记录 |
| `POST /api/runs/{id}/details` | `{description?, device_id?}` 补齐资料后恢复 |
| `POST /api/runs/{id}/approval` | IT 发送 `{decision: approve/reject, version, digest, reason?}` |
| `POST /api/runs/{id}/retry` | 重试模型失败；工单工具失败仅 IT 可重试 |
| `POST /api/runs/{id}/resume` | 恢复进程中断后尚未完成的节点；不能绕过待审批暂停点 |

## 真实实现与范围

- 提案把申请人、设备、工单动作、建议、来源版本纳入 SHA-256 摘要；审批必须同时匹配版本及摘要。
- 业务数据和 LangGraph 检查点使用同一个 SQLite 文件中的不同表；两条连接都在应用 lifespan 关闭。
- 单应用 `RLock` 串行保护完整的“读取当前状态—校验—恢复图”操作，防止并发重复确认。SQLite `tickets.run_id` 的唯一约束是写工单的最后一道防线。
- 本地工单工具在事务内核验审批及设备归属。遇到“工单事务已提交，但图还没记录成功”的错误，重试先查既有工单，再核验，不重复创建。若报障业务记录已提交但图的首个检查点尚未落盘就退出，恢复操作仅对 `collecting` 且确实没有检查点的任务从已存资料启动流程；不会重启已完成或已拒绝的任务。
- 只执行“创建本地演示工单”，没有运行系统命令、删除文件、重置密码、远程设备控制或任意工具选择。
- SQLite 和进程内锁适合单进程教学演示；生产多实例应重做锁/事务边界，换正式认证与审计方案，并对接真实 ITSM。接入外部工单 API 还需要上游幂等键、结果对账等措施，本项目没有声称对远程系统实现 exactly-once。

## 测试与读代码

```powershell
uv run pytest -q
```

测试使用临时 SQLite 文件、HTTP 测试客户端和模型 HTTP stub，不依赖外部服务。覆盖身份/设备归属、补问、审批拒绝、重复确认、旧版本、并发确认、关闭并重新打开数据库、工具失败重试、提交后报错恢复及模型异常。测试由 AI 辅助运行；这些结果不等于用户已逐项人工验收或已在生产环境验证。

按 [CODE_WALKTHROUGH.md](CODE_WALKTHROUGH.md) 的顺序阅读，不需要从 LangGraph 框架源码开始。
