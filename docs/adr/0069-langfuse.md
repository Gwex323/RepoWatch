# Langfuse 真实流程追踪（Issue #69）

## 范围与配置

使用可选依赖 `repowatch[observability]`（固定 `langfuse==5.0.4`），通过 Python SDK 的
`start_observation` 接口记录追踪。工作台启动时读取配置，变更后需重启后台。

| 配置 | 含义 |
| --- | --- |
| `REPOWATCH_LANGFUSE_ENABLED=1` | 显式启用；其他值均关闭 |
| `LANGFUSE_BASE_URL` | 必填，项目所在云端区域或自部署服务根地址，支持 HTTP/HTTPS，不允许内嵌凭据、查询或 fragment |
| `LANGFUSE_PUBLIC_KEY` | Langfuse 项目 Public Key |
| `LANGFUSE_SECRET_KEY` | Langfuse 项目 Secret Key，仅进入 SDK，不进入数据库、模型输入或 API 返回 |
| `LANGFUSE_TRACING_ENABLED=false` | 可额外强制关闭追踪 |

不自动部署 Langfuse，不自动读取 `.env`，不修改已运行进程的环境变量。
仅有 Langfuse 标准变量不会启用 RepoWatch 追踪，避免意外导出。

## 调用链与上下文

- `AnalysisModel._run` 为分析、环境诊断和修复分别建立一次 trace；流程包括模型配置检查及结果处理。
  `scope=model_flow` 明确覆盖模型流程，不代表后续持久化或 GitHub 发布成功。
- `task_binding` 通过 ContextVar 向新建 asyncio 任务传递任务、环境、仓库、提交和尝试编号，
  不修改持久化上下文、提示词或报告恢复绑定。Issue 编号在存在时作为整数记录。
- 每次完整重试/报告恢复产生新 trace，以同一 `task_id` 关联；模型请求重试属于同一 trace。
- 实际 Agent 生命周期记录为 `agent`，交接为 `agent.handoff`，本地函数工具为 `tool`。
- `ObservedModel` 记录 `generation`：模型名、供应商 ID、接口类型、请求序号、输出预算、
  推理档位、模型响应中的输入/输出 Token。自动重试的请求各有独立 generation。
- 保留安全的重试、报告终结和格式校验事件名称，不上传事件载荷。
- 每次分析/修复的本地事件保存 `langfuse_trace` ID；环境诊断可按原执行任务 ID 查询。
- Langfuse 元数据均为运行标识及参数，不代表复现成功、审查通过或已发布 PR。
  `output_produced` 仅指 Agent 返回输出，业务校验失败仍会将整个流程标记失败。

SDK 的异步生命周期回调可能由不同 asyncio 任务执行。因此所有 observation 显式传递
`trace_id` / `parent_span_id`，不用跨回调保持 OpenTelemetry 的上下文 token。
模型调用使用 try/finally 语义结束 observation；失败/取消时结束未完成的工具及 Agent。
每次流程独立保存 Agent、工具与调用计数，避免并发任务串线。

## 数据与故障边界

首版仅记录元数据，没有启用完整内容采集的配置项。源码、提示词、模型正文、工具参数与结果、
数据库内容、HTTP 请求头、服务 URL、密钥和异常原文均不进入追踪数据。
错误只记录异常类型、数值 HTTP 状态、白名单内的本地错误码和固定失败/取消状态，
不把异常原文传给 Langfuse；未知错误仅保留类型。

Langfuse 使用独立 TracerProvider，原有 Agents SDK `tracing_disabled=True` 保持不变，
不注册 OpenAI 默认出口，不对全部 HTTP 请求自动埋点，不重复使用供应商 API Key。
未启用时不导入 Langfuse 或初始化网络出口；配置缺失、SDK 不可用与初始化异常显示明确状态。
追踪操作异常被隔离并只记录固定本地警告；任务异常和取消仍原样传播，不被追踪吞掉。
导出在 SDK 后台批量执行；工作台关闭工作任务后在线程中关闭 SDK，避免在事件循环内同步 flush。

费用由 Langfuse 根据 Token 和模型定价计算，需自行核对中转供应商与自定义模型价格。
不把未知用量写成估算费用，不累计重复统计缓存或推理子类 Token。

## 验收与运行说明

「设置 → 系统信息」和受所有者登录保护的 `/api/system` 返回启用状态、原因、配置服务入口、
元数据模式及埋点异常标记，不返回项目密钥。初始化成功不代表连通性核验成功。

真实接入后可按以下步骤验收：

1. 在 Langfuse 创建项目并配置真实项目密钥，安装可选依赖，重启工作台。
2. 发起真实仓库分析，确认 trace 的任务标识、角色交接及工具层级与本地事件一致。
3. 确认每次模型请求独立显示耗时及响应提供的 Token，用量不重复累计。
4. 在失败或取消的实际任务中确认 observation 正确结束且业务状态保留。
5. 查看采集数据，确认无源码、提示词、模型正文、工具参数/结果及凭据。
6. 关闭开关并重启后确认没有新追踪，原任务能力可继续使用。

本次只接入追踪，不调整 120 秒请求上限、任务总时限、自动重试次数或修复发布行为。

## 本次交付检查与 review 记录

- 已完成：后端 Ruff 检查、Python 编译及模块导入、`pip check`、前端 TypeScript 检查与 Vite 构建、diff 空白检查。
- 本次未新增或运行测试；尚未部署 Langfuse、配置真实项目密钥或执行真实上报验收。
- 已逐项审查三条模型流程的调用入口、工作任务关联、生命周期回调、显式父子关系及异常结束路径。
- 已修正统一模型连接复用时供应商 ID 可能归属到首个角色的问题：请求元数据按当前实际角色解析。
- 已审查元数据白名单、错误文本隔离、未启用时的惰性依赖加载和可选依赖故障回退。
- 真实运行验收需在部署 Langfuse 后按上方步骤执行；静态检查与初始化状态不视为端到端上报通过。
