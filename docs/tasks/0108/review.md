# Issue #108 独立审查修正记录

- 日期：2026-10-31（Asia/Shanghai）。
- 分支：`feature/model-escalation-108`，基础提交 `5c25840`；Issue #108 已存在。
- 结论：本轮发现的阻断项已修正并复验，允许创建 RepoWatch 中文 PR；
  真实目标仓库验收尚未完成，不得据此宣称 `gateway-model-assistant` 已修复。

## 审查发现与修正

1. **阻断：未授权修复可能回退到监控模型。** 当策略开启但修复任务状态已失效时，
   `repair_agent` 原先会被映射到监控模型，仍存在用 flash 修改代码的可能。现在
   `ModelEscalation.apply` 在非维护分类的 `repair` flow 中失败关闭，返回
   `model_escalation_authorization_required`，不会创建模型执行者。
2. **阻断：服务端授权标记可能写入持久化上下文。** 修复阶段的内存授权标记现在
   在写入 `repair_tasks.context` 前明确移除；每次领取任务时也从旧上下文清除，
   再经过租约、来源、预算和账号检查后重新设置。
3. **中：策略引用失效时 `monitor`/`repair` 返回不完整对象，界面会显示
   `undefined / undefined`。** 后端现在返回 `null` 和明确缺失原因，前端不再
   猜测实际模型。
4. **中：升级策略不可用事件没有专门界面说明。** 修复时间线增加
   `model_escalation_unavailable` 标签和受限原因码展示。
5. **低：升级策略控件未继承父级禁用状态。** 监控、修复、启用和保存控件现在
   同时遵循父级 `disabled` 状态。

## 已验证

- `tests/test_model_escalation.py`：19 passed。
- `tests/test_model_providers.py`：33 passed。
- `tests/test_system_faults.py`：43 passed。
- `npx.cmd tsc --noEmit`：退出码 0。
- `npm.cmd run build`：受限环境外构建成功，Vite 输出 1753 modules。
- `ruff check`、`compileall`、`openspec validate --strict`：通过。
- 基线 `5c25840` 单独复现了 `test_repairs` 的验证键断言失败和协调维护组的
  失败；这些不是本次 diff 引入，不能记作 #108 通过或失败。

## 未验证范围

- 5.1、5.2、5.3 的完整 PR 发布器串联、预算耗尽、双恢复者和重复远程投递
  尚未全部补齐。
- `Gwex323/gateway-model-assistant` 的真实分析、v4pro 修复、flash 审查和 PR
  发布尚未执行。
- 尚未合并主干，未部署生产工作台。

在当前分支和已有离线证据范围内，审查阻断项为 0。

## 真实 flash 独立审查（恢复身份回归）

真实隔离分析在版本 9 暴露 `analysis_model_changed`：恢复身份在 escalation
应用前保存，却在应用后比较，启用策略后必然不一致。该问题进入第二轮修复和
独立审查，没有使用离线模拟替代真实模型结论。

| 时间（Asia/Shanghai） | 模型 | 结论 | 输出 |
| --- | --- | --- | --- |
| 2026-10-31 03:01:19 | `deepseek-flash` | 不通过，5 项 | `.codex-backups/flash-review-identity-108.json` |
| 2026-10-31 03:03:44 | `deepseek-flash` | 不通过，5 项 | 同上，已由后续审查覆盖 |
| 2026-10-31 03:06:14 | `deepseek-flash` | 不通过，4 项 | 同上，已由后续审查覆盖 |
| 2026-10-31 03:08:15 | `deepseek-flash` | **通过，0 项阻断** | 同上 |

最终审查摘要：`recovery_identity()` 和执行路径现在共用无副作用的
`ModelEscalation.apply_analysis()`，只基于规范化 analysis 路由计算身份；
策略启用状态、版本、monitor/repair 路由、有效模型配置和加密凭据均参与哈希，
而任务上下文、修复授权标记和路由中的非规范附加字段不参与。回复中只保留
3 项 P3 提示，不阻断合并。

针对审查意见的处理：

1. 当时读取策略路由时可能跳过上下文差异，现改为 canonical
   `apply_analysis()`，保存和执行都不读取 repair 上下文或 `force_monitor`。
2. `apply_analysis()` 与通用 `apply()` 共用 `_apply_routes()`，前者有独立
   的副作用测试；身份计算不再临时调用通用策略入口。
3. 直接替换供应商密钥和修改模型推理配置都会改变恢复身份；策略路由中不属于
   `provider_id + model` 的附加字段不会泄漏进身份摘要。
4. 补充无策略行、禁用策略、不同上下文、`force_monitor`、版本、路由交换和
   密钥轮换场景，保存身份和执行校验使用同一事务快照。

最终验证：

- `tests/test_model_escalation.py`：24 passed。
- 模型供应商、系统故障、报告恢复和修复流程联合回归：320 passed、3 failed。
- 上述 3 个失败均在提交 `796a46c` 的独立基线工作树复现：
  `test_resume_still_rejects_fabricated_evidence_and_persists_no_findings`、
  `test_confirmed_finding_runs_real_sdk_tools_and_handoff_then_publishes_once`、
  `test_unavailable_source_tool_can_recover_without_inventing_source[too_large]`；
  不能记为本次修复通过或本次引入的回归。
- `ruff check`、`compileall`、`git diff --check`：通过。

真实目标仓库的修复发布闭环仍未完成；PR #109 仍需先合并，才能进入隔离实例的
版本 9 重试和 `gateway-model-assistant` 实跑。
