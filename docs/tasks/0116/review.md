# Issue #116 分析重试、取消与旧记录复用修复记录

- 日期：2026-10-31（Asia/Shanghai）。
- Issue：<https://github.com/Gwex323/RepoWatch/issues/116>。
- 分支：`feature/analysis-runaway-retry-116`，从最新默认分支
  `origin/codex/initial-upload` 的 `0c4affe` 创建。
- 结论：实现、相关回归、系统故障测试、前端构建和本地代码审查通过；已创建
  中文 PR #117（目标分支 `codex/initial-upload`），当前开放且可合并，尚未
  合并、部署或重启工作台，真实目标仓库验收尚未执行。

## 根因

1. 请求超时只增加计数并继续重试，没有累计上限；超时耗尽后仍可能被最终报告
   自动恢复反复拉起。
2. 工具参数纠正没有有限额度，兼容供应商连续返回无效参数时可以无限调用。
3. 手动分析按 `trigger_key` 直接复用旧记录，取消或完成后的记录不会创建新任务。
4. 执行管线取消只处理已关联管线的分析，无管线分析继续持有租约并等待恢复。
5. 取消没有同步停止报告恢复，页面缺少取消和耗尽原因的中文说明。

## 实现

1. `MAX_TIMEOUT_RETRIES = 4`：初始请求之外最多四次超时重试，预算写入
   `analysis_report_recovery.retry_budget`，手动重试不会清零。
2. `MAX_TOOL_ARGUMENT_CORRECTIONS = 2`：工具参数、交接和必读工具纠正共用
   持久化额度；第三次无效响应进入 `analysis_tool_arguments_exhausted`。
3. 超时预算耗尽时，最终报告自动恢复以 `retry_exhausted` 停止，避免每个恢复
   周期再次发起请求。
4. 手动分析遇到任何已进入终态的同触发记录时，将旧记录转为 `superseded`，
   停止其恢复并创建新记录；活动记录仍保持幂等返回。
5. 环境停止或删除会取消该环境所有活动分析，原子同步 `cancelled`、租约归零、
   恢复停止，并写入 `analysis_cancelled` 审计事件。
6. 前端补齐 `analysis_cancelled`、耗尽原因和恢复停止原因；工具参数耗尽时
   优先显示最后一次参数或交接校验的具体原因。

## 审查发现与修正

1. 阻断：超时重试达到上限后，`analysis_provider_timeout_exhausted` 仍属于
   可自动恢复错误，恢复预算又不会重置，会形成周期性重复请求。现由
   `retry_exhausted` 终止恢复，并增加回归。
2. 中：前端把 `analysis_tool_arguments_exhausted` 全部按交接错误处理，覆盖
   了工具参数详情。现仅在存在交接或必读校验事件时展示对应原因，否则继续
   展示最后一次工具参数错误。
3. 中：取消路径只有数据库状态，没有分析事件。现写入
   `analysis_cancelled` 审计事件并在事件时间线显示。
4. 测试契约：DeepSeek、GLM、必读读取和旧超时测试仍假设无限纠正。已在未修改
   基线复现旧断言，再按有限额度更新，未放宽生产校验。

当前代码审查无阻断项。残余风险是超时预算为整条分析跨角色共享，且本轮没有
真实模型、浏览器端到端或生产工作台验收。

## 验证

- 最终拆分回归：456 passed，包含 43 项系统故障测试。执行管线的一项三秒
  等待断言在并行负载下曾超时；完整 `test_execution_pipeline.py` 串行复跑
  23 passed，确认不是生产行为回归。
- `tests/test_system_faults.py`：43 passed。
- `ruff check repowatch tests`：通过。
- `python -m compileall -q repowatch`：通过。
- `npm run build`：通过，Vite 转换 1753 个模块。
- `git diff --check`：通过。

## 未完成范围

- PR #117 已创建，当前尚未合并或部署，未重启端口 8765 的工作台。
- 尚未对 `Gwex323/gateway-model-assistant` 重新执行真实分析、修复、审查和
  发布验收。
- 本轮未运行浏览器端到端测试；前端仅完成 TypeScript 和 Vite 生产构建。
