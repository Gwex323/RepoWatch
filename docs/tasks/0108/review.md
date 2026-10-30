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
