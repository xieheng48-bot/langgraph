# 怎么读懂、修改和讲清这个项目

先用页面跑完“员工报障 → IT 审批 → 工单完成”，再按下面的顺序读。业务入口在自己的代码里，不需要一开始就啃框架。

这是 fork 中独立的应用工作流，业务规则由当前应用负责；LangGraph 提供执行、持久化检查点和中断恢复运行时。这里没有实现新的核心 Agent SDK 或 harness。作者及 AI 辅助实现/自动化测试说明见 [AUTHORS.md](AUTHORS.md)；下面的操作是学习和验收建议，不表示用户已经完成手动验证。

## 1. 看目录：每份文件负责一件事

```text
it_app/
  main.py        组装应用、身份校验、HTTP 路由、生命周期及并发锁
  models.py      请求字段和可信身份类型
  graph.py       状态、流程节点、分支、暂停与恢复
  repository.py  SQLite 业务表、设备/SOP、审批记录及幂等工单
  llm.py         离线规则建议与可选模型 HTTP 适配器
  index.html     中文演示页面；通过 API 操作，不直接改数据库
tests/
  test_service.py  权限、状态流转、审批、幂等、恢复
  test_llm.py      模型适配器、引用校验、失败停机和重试
```

## 2. 从 `main.py` 追踪一次请求

先找 `create_app()`。它注册路由，却不在模块导入时打开数据库。FastAPI 的 `lifespan()` 在服务启动时建立 `Runtime`，关闭时释放模型客户端和两条 SQLite 连接。测试可以用 `create_app(临时数据库路径)`，不会碰真实演示数据。

再看 `principal()`：它把 Bearer token 映射成 `Principal(user_id, role)`。`owner` 不由浏览器随意传入，更不会让大模型决定。`owned_run()` 拒绝普通员工访问别人任务，`check_device()` 校验设备归属。学习时可以尝试在请求体加入 `owner: bob`，服务端仍记录 Alice。

最后找 `report()`，顺序是：校验身份/设备 → 建立任务记录 → 调用 `graph.invoke(初始状态, config)` → 读取图状态和业务记录并返回。`config` 中的 `thread_id = it:<任务编号>` 将一个任务绑定到自己的检查点，不能用别人的任务 ID 共享执行状态。

`with runtime.lock` 包住整个读、校验和恢复操作。`SqliteSaver` 自己的锁只保护检查点读写，不自动保护“看见待审批 → 判断权限 → 恢复执行”这一整段业务逻辑。项目限定单进程，否则多个进程各有自己的锁，会失去这层保护。

## 3. 读 `graph.py`：应用怎样拆成可恢复节点

`ServiceState` 描述节点间流动的数据。它含任务身份、问题、设备、分类、来源、提案、审批版本/摘要、状态与工单 ID。

真正的 `StateGraph` 注册六个节点：

```text
START → collect → retrieve → propose → await_approval → create_ticket → verify → END
          ↓补问暂停           ↓模型失败结束    ↓拒绝结束      ↓工具失败结束
```

- `collect`：找缺失的描述/设备编号；需要补问就 `interrupt()`。资料齐全后服务端再次校验设备归属，按关键词分类。
- `retrieve`：通过 Repository 找出分类 SOP 和通用 SOP，返回带 ID 和版本的真实来源。当前是规则/分类检索，没有向量检索。
- `propose`：让 `Advisor` 提供参考文字，创建固定动作 `create_ticket` 的提案，并计算摘要。模型建议失败则终止到 `model_failed`。
- `await_approval`：保存暂停点，等 IT 人员发送批准或拒绝；批准才进入写工单节点。
- `create_ticket`：调用有限、明确的本地工具；已存在工单就复用。工具失败保留审批，等待人工核查和重试。
- `verify`：查回数据库，核对工单 ID、申请人、设备及审批摘要；成功才标记 `completed`。

可以在每个节点入口加调试断点，观察一个状态字段是谁写入的。不要把网页按钮上写的“已成功”当成任务真的成功，成功来自最终读回核验。

## 4. 读懂 `interrupt()` 与 `Command`

在 `await_approval` 节点，代码看起来像：

```python
answer = interrupt({"type": "approval", "version": version, "digest": digest})
```

第一次执行到这里，图会暂停并持久化检查点。之后 HTTP `/approval` 校验了 IT 身份和版本，再执行：

```python
graph.invoke(Command(resume={"decision": "approve", ...}), config)
```

恢复时该节点会**从头重新执行**，这次 `interrupt()` 返回 `resume` 的值。因此不能在 `interrupt()` 前写不可重复的外部副作用。当前节点先暂停，审批数据库写入在暂停返回后；Repository 也对同一审批作幂等处理。

`/details` 同理恢复缺资料补问。`/retry` 用 `update_state(..., as_node=...)` 明确安排失败步骤，不重新跑已经完成的报障与审批。`/resume` 用于已有检查点但未执行完的普通节点，不能绕过待人工输入的暂停点。此外还处理“业务任务已写入，但首个检查点尚未写入就退出”的窗口：只有业务状态为 `collecting` 且 `SqliteSaver.get_tuple()` 确认没有检查点时，才从已保存的可信身份和问题资料启动图；其他终态不会重新启动。

## 5. 为什么同时要版本、摘要和数据库唯一约束

三者解决不同问题：版本告诉审批人现在确认的是哪一版，摘要绑定具体内容，唯一约束防止同一任务落两张工单。

`propose` 用固定 JSON 编码后做 SHA-256，包含申请人、设备、固定动作、建议及来源版本。`/approval` 要求客户端提交当前 `version` 和 `digest`；过期或改过的确认返回 409，刷新后重新核对。

`Repository.decide()` 的条件更新再次检查版本和当前状态。`create_ticket()` 的同一数据库事务内检查审批人、决定、摘要与设备归属。`tickets.run_id UNIQUE` 作为最后一道防线。重复点击批准不等于再执行一个新任务。

看 `test_side_effect_committed_before_tool_error_is_not_duplicated`：工具先写成功工单，再模拟报错。重启服务后重试先发现已存在的工单，直接核验，最终只有一个 `ticket_created` 审计事件。这是本地数据库副作用的恢复例子，不代表任何外部系统都天然具有这一保证。

## 6. 模型在什么地方，为什么不能控制工具

`llm.py` 的 `Advisor.suggest()` 有两个明确模式。默认 offline 直接返回 SOP；llm 模式才通过 httpx 调用显式配置的兼容模型接口。测试使用 `httpx.MockTransport`，没有伪称调用了真实模型。

模型只返回 `advice` 和 `citation_ids`，代码校验 JSON 对象、建议长度以及引用 ID 是否来自检索结果。即使模型额外返回 `owner=bob` 或 `action=delete_files`，适配器也不会把它们传入提案。工单动作和归属由代码固定。

模型返回 JSON 格式正确、引用真实，也可能表达错误；审批页展示完整 SOP 供人工核对。文档内容和用户描述都被当作数据，不能据此改变服务端授权。不要在本项目基础上直接给模型一个“执行任意命令”的工具。

## 7. 推荐的修改练习

1. 新增“打印机故障”分类：改 `classify()`，新增一条带 ID/版本的 SOP，再写一个分类及引用来源测试。已有数据库的种子使用 `INSERT OR IGNORE`，新增 ID 会自动加入；修改已有 ID 内容不会覆盖已有数据，需要自己设计明确的数据更新方式。
2. 给报障增加 `occurred_at`：从请求模型 → 资料收集 → 状态 → 持久化 → 提案摘要 → 页面贯通。先自己写一张“字段流向”清单，再改代码。
3. 在不接真实公司系统的前提下增加本地工单查询工具，只允许查看当前申请人或 IT 可见的工单。先写跨用户查询测试，再实现。
4. 扩展失败测试：让模型返回不存在的来源，确认停在 `model_failed`，没有等待审批和写工单。
5. 未来再把分类检索替换成可评估的检索器：准备带期望 SOP ID 的问答集，先记录当前命中情况，再比较改动，不能先在简历写“准确率提升”。

## 8. 面试时如何按事实讲

可以说：“我做了一个个人 IT 服务台 Agent，真实使用 LangGraph 的状态图、人工中断和 SQLite 检查点，实现了审批摘要校验、工单幂等和进程重启后的恢复，并用自动测试覆盖越权、重复确认和失败重试。默认采用规则检索；另写了可配置的模型建议接口。”

还不能说：“已在公司生产上线”“万人使用”“准确率 95%”“多智能体自主修复”“对接真实飞书/企业 ITSM”“采用向量混合检索”。完成对应功能和实际测量后再更新简历。
