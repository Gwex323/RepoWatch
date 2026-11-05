# Agent 执行框架事实核查

调查日期：2026-11-02。本文保留访谈时的源码调查及候选判断，不是运行兼容性或性能测试结果。Q22 后已确认 LangGraph＋Deep Agents，当前决定见[设计基线](../design-baseline.md)，当前实施边界见[实施契约](../implementation-contracts.md)。

## 核查基线

| 对象 | 官方源码核查点 |
| --- | --- |
| Deep Agents Python SDK | 0.7.32，提交 `e85c6b98087861512d2c5240305e16cfac68a746`；[包声明](https://github.com/langchain-ai/deepagents/blob/e85c6b98087861512d2c5240305e16cfac68a746/libs/deepagents/pyproject.toml) |
| LangGraph Python | 1.2.19，提交 `5bd231cbfd913e05ebc662f519eb9a655bc766df`；[源码](https://github.com/langchain-ai/langgraph/tree/5bd231cbfd913e05ebc662f519eb9a655bc766df) |
| OpenHands Software Agent SDK | 1.59.0，提交 `ccae18cb897bef7960f210aa3f380b577a84be7c`；[包声明](https://github.com/OpenHands/software-agent-sdk/blob/ccae18cb897bef7960f210aa3f380b577a84be7c/openhands-sdk/pyproject.toml) |

以上是调查版本，不是本项目已锁定的依赖版本。滚动文档的内容应与源码核查点区分。

正式报告阶段通过 GitHub REST 再次核对当前 HEAD，版本声明仍为 Deep Agents 0.7.32、LangGraph 1.2.19、SQLite checkpoint 3.1.3 和 OpenHands SDK 1.59.0；本次 HEAD 与早期访谈的源码点不同，正式报告引用 `repository-snapshots.json` 中的固定提交。两组点用于记录调查过程，不视为已通过运行兼容测试。

报告复核还确认：Deep Agents 当前组装会加入文件和子 Agent 中间件，权限须检查最终工具／backend；异步子 Agent 路径涉及 LangSmith 部署，本项目使用本地／同步路径。LangGraph 当前仓库已有 SQLite Store；AsyncSqliteSaver 源码仍明确提示其生产负载限制，不能以存在该类来替代目标负载验证。

## Deep Agents 与 LangGraph

- `create_deep_agent()` 返回编译后的图，接受模型、工具、中间件、backend、checkpointer 与 Store；[固定实现](https://github.com/langchain-ai/deepagents/blob/e85c6b98087861512d2c5240305e16cfac68a746/libs/deepagents/deepagents/graph.py)。
- 子 Agent 可独立配置模型、提示词和工具，提供上下文隔离；上下文隔离本身不是进程或文件系统隔离，也不保证独立审查必经；[官方子 Agent 文档](https://docs.langchain.com/oss/python/deepagents/subagents)。
- 模型 API 的自定义地址与协议由模型适配层提供，需要对用户实际供应商测试，不能由配置入口推导全部兼容；[模型文档](https://docs.langchain.com/oss/python/deepagents/models)。
- checkpointer 保存图执行状态，依赖持久化后端与 `thread_id`；恢复可能重执行节点，外部副作用需要幂等处理；[持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[中断与副作用](https://docs.langchain.com/oss/python/langgraph/interrupts)。
- 文件 backend 与 shell 的隔离边界不同，本地 shell 工作目录不等于沙箱；[backend 文档](https://docs.langchain.com/oss/python/deepagents/backends)。
- 库可以自行运行，不以 Managed Deep Agents 为基础前提。Open SWE 当前应用的 MDA 部署要求属于该应用的部署条件，不能归给 Deep Agents 库；[SDK 概览](https://docs.langchain.com/oss/python/deepagents/overview)、[Open SWE 固定安装文档](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/docs/INSTALLATION.md)。

## OpenHands Software Agent SDK

- `Agent` 与 `Conversation` 提供模型和工具执行循环；[Agent 架构](https://docs.openhands.dev/sdk/arch/agent)。
- `LLM` 提供供应商、地址和接口模式配置；实际网关兼容性仍需验证；[固定实现](https://github.com/OpenHands/software-agent-sdk/blob/ccae18cb897bef7960f210aa3f380b577a84be7c/openhands-sdk/openhands/sdk/llm/llm.py)。
- `TaskToolSet` 及角色定义支持子任务与独立模型配置；[委派](https://docs.openhands.dev/sdk/guides/task-tool-set)、[角色定义](https://docs.openhands.dev/sdk/guides/agent-file-based)。
- 显式配置会话 ID 和持久化目录后，可以恢复会话状态和事件。这与 LangGraph 图 checkpoint 不同，不能推导外部命令回滚或没有重复执行；[会话持久化](https://docs.openhands.dev/sdk/guides/convo-persistence)。
- 支持本地与远程工作区，Agent Server 可在容器中运行；目录绑定本身不等于 OS 隔离；[工作区架构](https://docs.openhands.dev/sdk/arch/workspace)。
- SDK 可直接接模型 API，自行部署不要求 OpenHands Cloud 账号；[官方入门](https://docs.openhands.dev/sdk/getting-started)。

## 两者都需要宿主应用补齐

1. 跨仓库全局串行队列与可靠执行权。
2. 全仓扫描清单、覆盖证明、仓库准入和代码版本失效规则。
3. 修改前后验证、基线比较、独立审查必经和 PR 发布门槛。
4. 全局阻塞与人工恢复，并区分正常等待。
5. GitHub 远程写入的操作标识、结果核对与重复提交防护。
6. 仓库记忆与跨仓经验的作用域、去敏、失效和撤销。
7. Windows 后台服务包装、安装、进程监督和浏览器管理界面。

## 许可证

所核查的 Deep Agents、LangGraph 与 OpenHands SDK 库均为 MIT；托管服务与其他依赖另行核查。

- [Deep Agents LICENSE](https://github.com/langchain-ai/deepagents/blob/e85c6b98087861512d2c5240305e16cfac68a746/LICENSE)
- [LangGraph LICENSE](https://github.com/langchain-ai/langgraph/blob/5bd231cbfd913e05ebc662f519eb9a655bc766df/LICENSE)
- [OpenHands SDK LICENSE](https://github.com/OpenHands/software-agent-sdk/blob/ccae18cb897bef7960f210aa3f380b577a84be7c/LICENSE)

## 对本项目的候选判断

访谈当时，基于显式阶段、暂停恢复和程序验收需求，LangGraph＋Deep Agents 是优先候选；OpenHands SDK 在编码工具与工作区方面同样具有复用价值。该候选判断后来已由 Q22 收敛为 LangGraph＋Deep Agents，OpenHands SDK 保留备选；两者均未在本项目通过集成实验。
