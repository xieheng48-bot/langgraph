# 此 fork 的下游应用范围

此 fork 在保留 LangGraph 上游运行时的基础上，增加一个独立的业务应用：

| 路径 | 内容 | 维护归属 |
|---|---|---|
| [`downstream-apps/it-service-agent/`](downstream-apps/it-service-agent/) | 企业 IT 服务台：资料收集、SOP 检索、人工审批、本地工单、失败重试和持久化恢复 | 谢恒 / [xieheng48-bot](https://github.com/xieheng48-bot)，AI 辅助实现与自动化测试 |

应用使用 LangGraph 的公开执行、checkpoint 和 interrupt/resume API。业务流程、身份和审批策略由应用自己拥有；新增目录没有修改 LangGraph 核心运行时、checkpointer 实现或公共 API，也没有新建 Agent SDK 或 harness。上游 README、库代码、归档示例、许可证和作者记录保持原样。

运行命令、功能范围和局限请看应用的 [README](downstream-apps/it-service-agent/README.md)，作者及 AI 协作说明见 [AUTHORS](downstream-apps/it-service-agent/AUTHORS.md)。应用通过自己的 `pyproject.toml` 使用发布版框架依赖，可以独立启动和测试；应用自动化测试不代表上游库全套测试通过，也不表示用户已完成手动验收。

这是 fork 自有的学习与作品展示扩展，不属于上游官方功能或支持承诺。是否适合向上游贡献需要单独讨论与评审，不保证上游会接受这些应用文件。
