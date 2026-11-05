# E1 供应商能力验证

日期：2026-11-04。对应 [P0 任务 1.5](../../openspec/changes/build-github-issue-agent-harness/tasks.md)。当前已实现完整矩阵、分步诊断和本地 HTTP 契约测试；2026-11-04 无筛选完整矩阵 8／8 通过，22 次 HTTP 均为 200，费用仍待按账单核实。实际结果统一见 [P0 报告](../../experiments/results/p0.md)。

## 配置与启动

前置条件：已按[开发入口](README.md)安装锁定 Python 3.12 环境；有两家用户指定的供应商及明确的模型调用授权。实验入口不会查找其他软件的供应商配置、加载 `.env` 或读取个人目录中的凭据。

从项目根目录为实验建立本地配置：

```powershell
if (-not (Test-Path -LiteralPath experiments/providers.local.json)) {
    Copy-Item -LiteralPath experiments/providers.example.json -Destination experiments/providers.local.json
}
```

编辑 `experiments/providers.local.json`，为恰好两家供应商填写：

- `name`：唯一配置名称；由用户指定实际供应商，不把不同名称推断为不同服务商。
- `base_url`：完整 API 根地址，通常包含 `/v1`；接受 `http` 与 `https`。`http` 仅适用于本机或可信内网，凭据会以明文传输；公网供应商应使用 `https`。协议本身不因 `http` 拒绝，但拒绝 URL 中的用户名、密码、查询参数及片段，不通过重定向转发凭据。
- `credential_env`：仅此实验进程可用的环境变量引用，示例为 `REPOWATCH_E1_PROVIDER_A_KEY` 和 `REPOWATCH_E1_PROVIDER_B_KEY`；JSON 不接受明文 `api_key`。
- `configuration_revision`、`credential_revision`：配置及凭据修订，变更后递增。
- `processor`、`reviewer`：分别填写 `model`，可选填供应商支持的 `reasoning_effort`；不自动选模型或跨供应商回退。
- `chat_token_parameter`：可选填 `max_tokens` 或 `max_completion_tokens`；省略时，`api.openai.com` 使用后者，第三方 Chat 接口使用前者。Responses 固定使用 `max_output_tokens`。
- `tool_choice`：可选填 `auto` 或 `required`，默认 `auto`。兼容层按该显式策略处理 SDK 的强制工具选择，工具是否实际执行和结构化交接仍单独严格校验；失败不自动切换策略。修改配置策略时递增 `configuration_revision`。

通过受信任终端或凭据管理器将指定变量注入本次 Python 进程。密钥不写入配置、提示词或报告；本地配置及运行报告均已忽略。该引用方式仅用于 P0 实验，不替代 P1 的宿主受保护凭据端口。

运行会产生有界的真实模型调用：

```powershell
backend/.venv/Scripts/python.exe experiments/run.py e1
```

也可用 `--provider-config` 显式选择实验配置文件。示例里的域名和模型为占位，须先替换。缺配置、格式错误或任一凭据缺失时整轮预检拒绝调用，不尝试其余已配置供应商；返回退出码 `2`，生成未运行矩阵。只有真实运行的全部组合通过时返回 `0`。

## 按组合分步诊断

原有配置可直接使用，无需更换地址、模型或密钥。先在已注入凭据的同一 PowerShell 中选择一个实际配置名称、接口及角色：

```powershell
backend/.venv/Scripts/python.exe experiments/run.py e1 --provider laogou8 --api chat_completions --role processor --stage connection
```

基础连接通过后，分别把 `--stage connection` 改为 `--stage tools`、`--stage handoff`，每次查看报告再执行下一步；失败时先停止后续调用。火山配置可将 `--provider` 改为其实际名称，如 `"火山引擎"`；以参考项目成功使用的接口为准，Responses 使用 `--api responses`。

| 阶段 | 实际请求及通过含义 |
| --- | --- |
| `connection` | 与参考项目连接测试相同的最小 HTTP 请求，无工具，最大输出 32 tokens；校验基本模型响应格式 |
| `tools` | 经过 langchain-openai 的真实 SDK 工具调用，串行执行处理／只读审查夹具；不请求最终结构化结果 |
| `handoff` | 经过 Deep Agents／ToolStrategy，校验必需工具、完整版本上下文及随机 proof；也是未指定阶段时的默认行为 |

过滤组合或使用前两个阶段时，报告 `diagnostic_only=true`，未选组合标为 `not_run/not_selected`。仅要求所选供应商凭据，名称不匹配时零调用。所选诊断成功可返回退出码 `0`，但 `gate_passed` 始终为 `false`；退出码不代表完整 E1 已通过。只有不加组合过滤、执行全部 8 项 `handoff` 的真实矩阵可以通过 E1 门槛。

基础连接、工具、交接是独立诊断命令，不会自动连续执行、切换接口或回退。确认单组合兼容后，再运行不带筛选的完整 `e1`。

## 能力矩阵

固定测试两家供应商各自的 `chat_completions`／`responses` 与 `processor`／`reviewer`，共 8 个组合。接口选择显式传给 langchain-openai，接入现有 Deep Agents 本地图及 ToolStrategy，不以普通 HTTP／文本回复判定就绪。

| 角色 | 必需工具活动 | 交接验证 |
| --- | --- | --- |
| 处理 | 读取 `fixture.py`，用 `write_source` 将内存加法夹具由减号改为加号 | 返回当前信封的完整上下文及工具返回的随机 proof |
| 独审 | 读取源码及只读模拟前后证据，不发放源码写权限 | 独立会话返回当前信封、角色模式及相同 proof |

框架写文件、通用终端及未绑定子任务仍受模式工具入口拒绝。这里不接触真实仓库或磁盘源码；Git 标识、前后结果及代码均为能力夹具，不作为修复成功证据。最终结果须通过既有 `validate_result`，缺工具活动、错误任务／运行／代码版本、空泛文本及错误 proof 均不能通过。

交接阶段使用专用的可信系统指令，明确 `summary` 是挑战回传值，不是任务说明。夹具工具返回 `handoff_summary`，其值为 `probe:` 加本轮随机 proof；模型须逐字复制到 `StructuredResult.summary`。该值只经实际工具响应提供，不预填到任务或输出 schema，不由程序代填、去空格或纠错。必需工具和原始 proof 仍精确校验。

## 消耗与失败

示例配置限定整轮最多 32 次模型调用尝试、每组合最多 4 次、每次最大输出 2048 tokens（基础连接为 32）。SDK 自动重试为 0；不同组合独立测试，不把失败组合切换到其他协议或供应商。`model_calls` 在 SDK／基础请求开始前计数，不等于供应商已受理或计费次数；HTTP 是否发起、是否收到响应须另看 `http` 字段。

单次请求的网络超时为 30 秒，新调用前还核对整轮 300 秒时限；达到调用或时限上界后不再发出新请求。此处使用 SDK 网络超时和调用前检查，不承诺进程级硬终止；正式执行预算、未知用量预留及旧调用停止核验仍由 P1 建立。

每组合保存调用尝试、供应商返回的输入／输出 tokens、缺失用量调用数、返回的模型标识、角色与配置／凭据修订、工具活动及耗时。HTTP 失败另存数字 `http_status`；能力校验失败保存受控原因 `failure_detail`（如 `summary_proof_mismatch`），不保存响应正文或原始异常。未返回用量或响应失败时保留未知，不当作零消耗。`actual_cost` 保持 `null` 并注明未读取账单，不套用其他供应商价格或把估计当作扣费事实；用户可按报告修订与时间核对供应商账单。

每组合的 `http` 另记录 `requests_started`（HTTP 发送前钩子，不能证明服务端已接收）、`responses_received`、`successful_responses`、`status_codes`、有界格式的 `request_ids`、白名单 `provider_codes`／`rejected_parameters` 及固定分类 `error_hints`。已知“无可用渠道”返回 `no_available_channel`，用于区分网关路由拒绝与模型推理失败；没有该提示不能反推路由正常。截断依据响应的 `finish_reason=length` 或 Responses 的 `incomplete_details.reason=max_output_tokens` 保存为 `output_truncated`，不单凭 token 数推断。控制台没有日志时，可先查看上述脱敏证据。

取得已通过版本归属校验的结构化结果后，`handoff` 仅保存摘要长度、精确匹配／含 proof／前缀匹配／有外围空白等布尔或数字信息，不保存模型摘要原文或随机 proof。`summary_proof_mismatch` 表示该字段尚不满足挑战协议，不能把它解释为连接、工具或整个结构化接口不可用。

结构化交接失败时，`handoff` 保持 `null`，另存 `handoff_diagnostics`：`kind` 区分未产生结构化结果、字段校验失败、上下文不匹配或其他值错误；`final_tool_names` 为有界工具名列表，`final_text_length` 为最终文本长度；字段校验仅保存错误路径和类型码（如 `context.task_id:missing`），不保存字段值、模型文本或异常正文。该诊断只用于定位适配差异，不能替代完整 E1 通过。

失败仅保存鉴权／权限、限流／额度、HTTP／协议、超时、无效交接、越权或实验上限等固定类别。报告不保存密钥、原始提示词／响应、SDK 异常正文或 URL；实验关闭 tracing，不继承环境代理。保存配置本身不产生任何模型或列表请求。

## 报告与本地检查

每次 E1 命令在 `experiments/results/local/e1-<UTC 时间>.json` 保存独立报告，包含配置摘要、版本、限制、8 项矩阵和门槛结论。当前 `adapter_revision=4`，共用 P1 正式网关适配，保留参数及交接提示策略并增加串行请求保护；旧报告不补造新字段。`execution_mode=http_fixture` 的注入 HTTP 夹具即使能力测试通过，`gate_passed` 仍为 `false`；真实供应商与完整 P0 是否通过分别记录，不修改产品准入或恢复状态。P1 本轮真实复测状态见[P1 供应商](providers.md#当前验证与真实-e1-复测)。

运行不消耗真实额度的契约测试：

```powershell
backend/.venv/Scripts/python.exe experiments/run.py e1-contracts
```

测试实际经过 SDK、两类 HTTP 请求、Deep Agents、工具和结构化结果，HTTP 响应由本地夹具返回。这能验证适配逻辑，不能代替真实供应商 E1。

接口格式核对来源：[OpenAI 官方 Function calling 文档](https://developers.openai.com/api/docs/guides/function-calling)，于 2026-11-03 检索并读取；第三方兼容性仍以各自实际结果为准。

2026-11-03 对照 [Gwex323/repowatch 的连接测试](https://github.com/Gwex323/repowatch/blob/7dde87fce2729d3439e0b27d200e372cc7f16dc8/repowatch/provider_probe.py#L116)及其供应商保存与运行适配源码，确认保存、连接测试和实际 Agent 调用分开。当前沿用本项目已确认的 LangGraph／Deep Agents 路线，只对齐最小请求参数及诊断分层，不引入该项目的 OpenAI Agents SDK 或推断新适配器已经真实兼容。
