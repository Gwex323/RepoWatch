# 相关项目实现对比与固定源码依据

调查日期：2026-11-02。范围为官方 GitHub 仓库及官方文档的只读核查，没有安装或运行项目。固定提交来自 [GitHub REST 快照](repository-snapshots.json)；“不覆盖本项目要求”指核查的职责和默认流程不提供该保证，不表示项目不能通过二次开发扩展。

## 1. Deep Agents＋LangGraph：已选库级底座

**结构与流程。** Deep Agents 的 `create_deep_agent()` 组装模型、工具、中间件、文件 backend 与子 Agent，返回 LangGraph 图；LangGraph 提供阶段执行、持久化和中断原语。宿主可注入 checkpointer、Store、模型和自定义工具。主 Agent 与子 Agent 能有不同模型与上下文，但角色独立不等于底层文件系统和进程已经隔离。

**可复用。** 模型／工具循环、上下文管理、同步子任务、流事件、图状态和人工中断。与本项目的显式阶段和本地 Worker 组织方式匹配。

**需补齐。** 跨仓唯一执行权、扫描覆盖证明、受控 Runner、独立审查必经、发布门槛、全局阻塞、远端对账和记忆治理均归宿主应用。默认中间件与通用子 Agent 需要显式收窄权限。本项目采用本地／同步子任务，不使用依赖托管部署的异步子 Agent 路径。

**版本与许可。** Deep Agents 0.7.32、LangGraph 1.2.19，源码许可 MIT。源码点不是已经通过集成验证的依赖锁。

依据：[Deep Agents 入口](https://github.com/langchain-ai/deepagents/blob/311ed7fb0e496b93ca56fb0b8e46ed0e5355600f/libs/deepagents/deepagents/graph.py)、[包声明](https://github.com/langchain-ai/deepagents/blob/311ed7fb0e496b93ca56fb0b8e46ed0e5355600f/libs/deepagents/pyproject.toml)、[LangGraph 包](https://github.com/langchain-ai/langgraph/blob/ee3c043bddcc514012885167dd5916b15d6e4731/libs/langgraph/pyproject.toml)、[SQLite Store](https://github.com/langchain-ai/langgraph/blob/ee3c043bddcc514012885167dd5916b15d6e4731/libs/checkpoint-sqlite/langgraph/store/sqlite/base.py)。

## 2. OpenHands Software Agent SDK：库级备选

**结构与流程。** Agent、Conversation、LLM、Tool 和 Workspace 是主要抽象。会话驱动模型与工具循环；本地或 Agent Server 远程工作区执行动作，事件和会话状态可持久化。Task 委派支持专门子任务与独立角色配置。

**可复用。** 编码工具、工作区、事件模型和模型适配。相比直接复用整个平台，SDK 更适合作为宿主 Harness 的执行器候选。

**需补齐。** 会话保存不等于业务工作流事务或外部命令回滚；还要实现本产品的队列、覆盖、证据、暂停和 GitHub 生命周期。可选 Critic 不能直接充当已验证修复门槛。

**版本与许可。** 1.59.0，Python ≥3.12，SDK 为 MIT；该许可不外推为 Cloud／Enterprise 服务的许可。自行运行 SDK／Agent Server 不以 OpenHands Cloud 账号为基础前提。

依据：[README](https://github.com/OpenHands/software-agent-sdk/blob/98849457621881ae2414719b82b8bc1cf535b4ab/README.md)、[包声明](https://github.com/OpenHands/software-agent-sdk/blob/98849457621881ae2414719b82b8bc1cf535b4ab/openhands-sdk/pyproject.toml)、[LLM 实现](https://github.com/OpenHands/software-agent-sdk/blob/98849457621881ae2414719b82b8bc1cf535b4ab/openhands-sdk/openhands/sdk/llm/llm.py)、[会话持久化文档](https://docs.openhands.dev/sdk/guides/convo-persistence)、[工作区文档](https://docs.openhands.dev/sdk/arch/workspace)。

## 3. mini-SWE-agent：简洁执行循环

**结构与流程。** DefaultAgent、Model 和 Environment 解耦。任务消息 → 模型查询 → 执行命令 → 追加观察，直到完成、异常或达到限制；配置轨迹路径后可逐轮保存。

**Agent 组织。** 基本运行是单 Agent／模型／环境，批量运行任务不是协作式多 Agent 团队。

**可复用与缺口。** 适合借鉴循环、费用限制、模型适配和轨迹设计，或用作简单执行基准。没有本项目的 Web 服务、跨仓队列、覆盖账本、独立审查门槛和 PR 生命周期；轨迹文件也不自动提供服务级恢复和长期记忆。

**许可。** MIT。

依据：[固定 README](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/README.md)、[循环](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py)、[模型适配](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/models/litellm_model.py)、[本地环境](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/environments/local.py)、[许可证](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/LICENSE.md)。

## 4. gh-aw：Actions 工作流与受控写入

**结构与流程。** Markdown 描述任务，frontmatter 定义触发、权限、工具和引擎，编译器生成 `.lock.yml`，由 Actions 执行。Agent 准备修改和产物，单独的 safe-outputs 作业以限定权限校验并执行远端写入。

**Agent 组织。** 工作流／作业是主要组织单位，可增加检测 Agent 或工作者，但不是每个固定角色独立供应商的通用协作框架。“默认只读”主要指 GitHub 远端访问，不代表工作区完全不能修改。

**可复用与缺口。** 借鉴权限分离、产物交接、声明式规则和程序发布器。安全输出校验不等于完成复现、回归和独立审查；该版部分检测采用 warn 策略，也不能直接视为严格阻断。Actions 执行形态与本地 Worker、记忆、恢复需求不同。

**许可。** MIT。

依据：[README](https://github.com/github/gh-aw/blob/f37d3b9ab855215f70434caf4b7523e28466615f/README.md)、[编译器](https://github.com/github/gh-aw/blob/f37d3b9ab855215f70434caf4b7523e28466615f/pkg/workflow/compiler.go)、[safe-outputs 作业](https://github.com/github/gh-aw/blob/f37d3b9ab855215f70434caf4b7523e28466615f/pkg/workflow/compiler_safe_outputs_job.go)、[仓库记忆](https://github.com/github/gh-aw/blob/f37d3b9ab855215f70434caf4b7523e28466615f/docs/src/content/docs/reference/repo-memory.md)、[许可证](https://github.com/github/gh-aw/blob/f37d3b9ab855215f70434caf4b7523e28466615f/LICENSE)。

## 5. Claude Code Action：GitHub 场景执行适配

**结构与流程。** Action 解析事件／权限，选择交互或显式任务模式，准备上下文、分支和工具，安装并调用 Claude Code，回写进度和结果。Action 层主要组织一次执行会话。

**默认 PR 行为。** 交互提示词要求推送修改后提供预填 PR 创建链接；开放 PR 可更新现有分支。自定义自动化可配置创建 PR，但不是全部场景的统一默认行为。

**可复用与缺口。** 借鉴 GitHub 上下文、分支策略和单条进度评论。多种云接入渠道仍是 Claude 的接入渠道，不能据此称其具备任意角色多供应商模型编排；本项目已选择纯模型 API 的本地 Harness，需要不同的执行底座和业务门槛。

**许可。** Action 源码 MIT，不外推至 Claude Code 产品本身。

依据：[README](https://github.com/anthropics/claude-code-action/blob/d535f5172dec02b204866b393d6b20b9b2aaf98d/README.md)、[执行入口](https://github.com/anthropics/claude-code-action/blob/d535f5172dec02b204866b393d6b20b9b2aaf98d/src/entrypoints/run.ts)、[SDK 调用](https://github.com/anthropics/claude-code-action/blob/d535f5172dec02b204866b393d6b20b9b2aaf98d/base-action/src/run-claude-sdk.ts)、[PR 提示词](https://github.com/anthropics/claude-code-action/blob/d535f5172dec02b204866b393d6b20b9b2aaf98d/src/create-prompt/index.ts)、[许可证](https://github.com/anthropics/claude-code-action/blob/d535f5172dec02b204866b393d6b20b9b2aaf98d/LICENSE)。

## 6. Probot：GitHub App 事件框架

**结构与流程。** HTTP 接收 Webhook，进行校验和转换，分发到 `app.on(...)` 处理器，通过认证 Octokit 调用 GitHub。事件处理器和插件不是内置 Agent。

**可复用与缺口。** 可借鉴认证身份、权限、事件路由和错误处理；如果以后增加 Webhook，可作为可选接入参考。它没有模型循环、工作区、任务恢复、验证、记忆及 Web 管理产品；当前 Python 后端和本地轮询不必为此另引一套 Node.js 服务。

**许可。** ISC。

依据：[README](https://github.com/probot/probot/blob/a20630b25a325184ccc35b0bf79e9f00a7d5938d/README.md)、[核心类](https://github.com/probot/probot/blob/a20630b25a325184ccc35b0bf79e9f00a7d5938d/src/probot.ts)、[Webhook 构造](https://github.com/probot/probot/blob/a20630b25a325184ccc35b0bf79e9f00a7d5938d/src/octokit/get-webhooks.ts)、[许可证](https://github.com/probot/probot/blob/a20630b25a325184ccc35b0bf79e9f00a7d5938d/LICENSE)。

## 7. Open SWE：工程应用与托管运行时

**结构与流程。** Web dashboard、FastAPI 集成／授权层、PostgreSQL 业务记录和任务队列构成应用层；`mda/` 承载 Deep Agents 循环、工具、中间件与检查点。Issue 或评论提及机器人 → 仓库及身份检查 → MDA thread → 调查、修改和检查 → push → 专用工具创建 PR。独立 reviewer 可用，CI babysit 需要主动启用，不代表每次发布前强制独审。

**运行时边界。** 此版本启动代码强制检查 `MDA_RUNTIME_URL` 及签名配置，切换 sandbox 不能解除 MDA 依赖。安装路径还涉及 GitHub App、Slack、PostgreSQL；独立 Docker 路径列出 Redis、LangSmith 与 LangGraph 许可键。因此“可部署应用”不能直接解释为“免 MDA 的纯本地库运行”。这与本项目直接嵌入 Deep Agents／LangGraph 库的路线不同。

**可复用与缺口。** 借鉴任务会话、PR 关联、后续反馈、租约及心跳。其并发与过期租约自动接管策略、sandbox 内的 GitHub 权限，与本项目全局串行、异常后人工恢复及写凭证留宿主的边界不同。检查提示词和发布工具也没有提供本项目要求的完整修复证据门槛；全仓覆盖及预算内自动补全仍需自建。

**许可。** 自有代码 MIT；根许可不覆盖 `vendor`，所核对的 MDA SDK 与 QuickJS 各带 MIT 许可，需保留对应声明；服务使用权另计。

依据：[README](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/README.md)、[MDA 客户端](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/openswe/agent_api/client.py)、[GitHub 入口](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/openswe/github/channel.py)、[交付提示词](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/openswe/resources/prompts/system/commit-pr.md)、[发布工具](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/openswe/tools/open_pull_request.py)、[CI 跟进](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/openswe/baby_sit.py)、[调度](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/openswe/jobs/worker.py)、[安装说明](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/docs/INSTALLATION.md)、[根许可](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/LICENSE)、[vendor 边界](https://github.com/langchain-ai/open-swe/blob/80877db7f6410ee6a3a8bccdead705132e2cfd92/vendor/README.md)。

## 8. OpenHands Canvas＋Automation：界面与通用运行管理

**主仓职责。** 当前固定版本的 OpenHands 主仓是 React／TypeScript Agent Canvas，管理会话、文件、后端选择与自动化界面；SDK／Agent Server、TypeScript client 和 Automation 已分仓。Issue→PR 不是 Canvas 本仓的一条内建固定流水线，运行由后端负责。支持本地及 Docker／自托管后端，Cloud 可选；安装工具要求包括 Node ≥24、uv，启动器可配置 SQLite。

**Automation 结构与流程。** FastAPI＋SQLAlchemy／Alembic；cron 或 webhook → 事件匹配及去重 → 持久 PENDING → dispatcher → 本地 Agent Server 或云 sandbox → 配置脚本／Agent → 完成回调。PR 创建和 CI 返修取决于任务配置。README 主打 Cloud／PostgreSQL，但源码也支持本地 Agent Server＋SQLite，不能仅凭宣传页断言必须云端运行。

**Agent 与恢复。** 触发和运行管理层不等于固定多 Agent 编排。watchdog 会核对超时运行的命令状态，适合借鉴；自动停用主要作用于单个 automation，dispatcher 可并发，manual 触发另有条件。没有核实到本项目所要求的跨仓全局故障联锁和人工恢复协议。

**可复用与缺口。** 可借鉴后台与浏览器分离、事件入库、运行分发及命令核对。若整体复用，仍须补覆盖账本、受控补全、修复前后证据、强制只读独审、版本绑定门槛、全局暂停及远端不明结果对账。SDK 是独立的库级候选，见第 2 节。

**许可。** 本次固定版本的 Canvas 与 Automation 自有代码均为 MIT，依赖与云服务分别核对。

依据：[Canvas 分仓说明](https://github.com/OpenHands/OpenHands/blob/59b18013ee2535950860460e90e898c3cf05a9e4/README.md)、[架构](https://github.com/OpenHands/OpenHands/blob/59b18013ee2535950860460e90e898c3cf05a9e4/docs/architecture.md)、[启动器](https://github.com/OpenHands/OpenHands/blob/59b18013ee2535950860460e90e898c3cf05a9e4/scripts/dev-with-automation.mjs)、[Canvas 许可](https://github.com/OpenHands/OpenHands/blob/59b18013ee2535950860460e90e898c3cf05a9e4/LICENSE)、[Automation README](https://github.com/OpenHands/automation/blob/407bf8a021cce90656ee8d92292bd742b550080f/README.md)、[事件入库](https://github.com/OpenHands/automation/blob/407bf8a021cce90656ee8d92292bd742b550080f/openhands/automation/ingest.py)、[SDK 运行入口](https://github.com/OpenHands/automation/blob/407bf8a021cce90656ee8d92292bd742b550080f/openhands/automation/presets/prompt/sdk_main.py)、[配置](https://github.com/OpenHands/automation/blob/407bf8a021cce90656ee8d92292bd742b550080f/openhands/automation/config.py)、[watchdog](https://github.com/OpenHands/automation/blob/407bf8a021cce90656ee8d92292bd742b550080f/openhands/automation/watchdog.py)、[派发](https://github.com/OpenHands/automation/blob/407bf8a021cce90656ee8d92292bd742b550080f/openhands/automation/dispatcher.py)、[单自动化停用](https://github.com/OpenHands/automation/blob/407bf8a021cce90656ee8d92292bd742b550080f/openhands/automation/utils/unhealthy.py)、[Automation 许可](https://github.com/OpenHands/automation/blob/407bf8a021cce90656ee8d92292bd742b550080f/LICENSE)。

## 9. PR-Agent：命令驱动的审查工具集

**结构与流程。** Python 入口接收命令 → 读取仓库配置及请求策略 → 按 `command2class` 路由 → 执行工具 → 解析、输出。`review`、`describe`、`improve` 是不同工具类，不能仅凭项目名称将其解释为固定多 Agent 团队。

**上下文处理。** PR 描述、提交信息、diff、关联工单、配置的仓库文件和既往发现组成上下文；按 token 预算扩展、压缩或分块 diff，再合并结果。CLI 可接收 stdin／diff 文件并输出 JSON，适合作为本地审查接口参考。源码存在模型回退路径，若复用需遵守本项目禁止自动跨供应商切换的规则。

**可复用与缺口。** 可借鉴 Git 平台适配、diff 预算、结构化审查和评论去重。其主链是 PR 分析与建议，不提供本项目完整的 Issue 修改、受控复现、全仓覆盖、全局串行及异常恢复语义。

**许可。** MIT。旧 `qodo-ai/pr-agent` 在调查时重定向到 `The-PR-Agent/pr-agent`，本报告只计为同一个项目。

依据：[命令路由](https://github.com/The-PR-Agent/pr-agent/blob/c6a076d6f9fb59029341b0b4f5662ed522d11589/pr_agent/agent/pr_agent.py)、[审查与上下文](https://github.com/The-PR-Agent/pr-agent/blob/c6a076d6f9fb59029341b0b4f5662ed522d11589/pr_agent/tools/pr_reviewer.py)、[CLI](https://github.com/The-PR-Agent/pr-agent/blob/c6a076d6f9fb59029341b0b4f5662ed522d11589/pr_agent/cli.py)、[许可](https://github.com/The-PR-Agent/pr-agent/blob/c6a076d6f9fb59029341b0b4f5662ed522d11589/LICENSE)。

## 10. Kodus：分阶段审查流水线

**结构与流程。** TypeScript／NestJS 后端拆分 API、Webhook、Worker，配 Web、PostgreSQL／pgvector 和 MongoDB。审查流水线依次完成前置检查、配置、变更及外部上下文／CI 加载、业务校验、沙箱与分析器、Agent 审查、建议验证和评论／审批。

**实际 Agent 组织。** 普通模式用 generalist 覆盖启用类别；deep 模式拆为 bug／security／performance 专家，规则和自定义 Agent 可额外加入，经 `Promise.allSettled` 并行执行。文件正文从初始输入剥离，由 Agent 按需在沙箱读取，自定义角色按文件范围裁剪上下文。内部有上下文压缩、覆盖账本和 finder→verify；审查覆盖和建议验证不等于修改后修复验收。

**可复用与缺口。** 借鉴专业审查、范围化上下文、分阶段产物和结果去重。并行审查不是本项目必须照搬的 Agent 数量。部署重于本地 SQLite 方案；全仓导入扫描、Issue 实现、证据发布门槛及全局恢复仍需自行实现。

**许可边界。** GitHub 元数据 `NOASSERTION` 不是许可正文。固定版本 `license.md` 规定：非企业源码为 AGPL-3.0，文件名含 `.ee.` 或位于 `ee/` 的代码适用专有企业许可。`license_ee.md` 在软件限制内允许原样自托管 Community 功能及 BYOK 用于内部生产，不授予修改后部署、分发或并入其他产品的通用权利，评估修改另有有限范围。流水线装配文件本身是 `.ee.ts`。本项目以思想借鉴为主；直接代码复用须逐文件核对适用许可和依赖，不能整体搬用。

依据：[部署](https://github.com/kodustech/kodus-ai/blob/03b52a7e16b177b7024283f37d21023b604d8398/docker-compose.prod.yml)、[流水线](https://github.com/kodustech/kodus-ai/blob/03b52a7e16b177b7024283f37d21023b604d8398/libs/code-review/pipeline/strategy/code-review-pipeline.strategy.ts)、[review orchestrator](https://github.com/kodustech/kodus-ai/blob/03b52a7e16b177b7024283f37d21023b604d8398/libs/code-review/infrastructure/agents/review-orchestrator.service.ts)、[执行循环](https://github.com/kodustech/kodus-ai/blob/03b52a7e16b177b7024283f37d21023b604d8398/libs/code-review/infrastructure/agents/core/core-agent-loop.adapter.ts)、[企业装配文件](https://github.com/kodustech/kodus-ai/blob/03b52a7e16b177b7024283f37d21023b604d8398/libs/core/providers/code-review-pipeline.provider.ee.ts)、[license.md](https://github.com/kodustech/kodus-ai/blob/03b52a7e16b177b7024283f37d21023b604d8398/license.md)、[license_ee.md](https://github.com/kodustech/kodus-ai/blob/03b52a7e16b177b7024283f37d21023b604d8398/license_ee.md)。

## 11. 从项目事实到本项目取舍

上述架构、入口、许可及默认流程来自对应固定版本；“推荐复用什么”“需补齐什么”是结合 Q1—Q36 的工程判断。未开展效果排行或兼容性测试，不能把源码中出现工具、持久化或验证模块解释为相关硬约束已经满足。

本项目采用两类核心 Agent，是因为处理与独立审查需要不同写权限和会话边界；扫描、调查、规划、复现和实现可共享处理执行引擎。其他项目使用单循环、工具路由或多专家的事实，不能推出 Agent 越多越好。子任务通过 Harness 结构化结果交接，程序掌握队列、证据与发布，不需要另设常驻调度、测试执行或恢复 Agent。
