# PR #60 冲突处理与 review 记录

日期：2026-10-28。关联 Issue #76，原功能 Issue #59 / #57，沿用 PR #60。

## 概述

从最新默认主干 `b6a9214b06877109af81f83a9b7c934aecf0965e` 创建
`feature/pr60-conflicts-76`，整合原 PR 头提交
`944568db90aa3e9b6dc1518ea4e116472c3db9c9`。保留双方历史，更新原 PR 时使用普通快进推送。

## 变更内容

- 合并 `frontend/repair.tsx` 的交接恢复和审查返工恢复字段、提示。
- 合并 `repairs.py` 的恢复资格、重试门禁和事件，保留主干的模型变更授权与误报关闭逻辑。
- `RepairStageModel` 用 `ready_for_review` 判断后端交接，保留主干真实 SDK 交接和证据读取。
- 保留有界审查返工循环，每轮重新验证并读取实际证据；验证、审查纠正计数共同影响有限调用预算。
- 测试模拟器按输出契约识别最终审查，避免依赖后端交接前的旧固定请求序号。

## 设计方案/说明

不能仅凭上一轮 `verification.passed` 就再次交接。独立审查拒绝后，
`workspace.review` 不为空，`ready_for_review` 为假；必须提交不同候选并重新验证，
才会清除旧审查并重新开放交接。每轮独立审查通过真实工具读取当前验证结果，
不能将模型正文当作证据读取。

修复角色保留主干的 10 次初始探索调用；验证纠正和审查纠正各增加有界的调用空间。
纠正次数、源码读取、输出/超时重试及工作区期限仍共享，未增加审查返工次数。

## 验证/验收

最终针对性离线回归：102 项通过，37.06 秒。

```powershell
python -m pytest tests/test_repair_review_feedback.py tests/test_acceptance_recovery.py tests/test_repair_feedback.py tests/test_repairs.py::test_confirmed_finding_runs_real_sdk_tools_and_handoff_then_publishes_once tests/test_repairs.py::test_output_recovery_reduces_reasoning_and_keeps_it_for_later_calls tests/test_repairs.py::test_output_recovery_preserves_default_and_already_low_reasoning -q --tb=short
```

Ruff 全后端/测试检查、Python compileall、TypeScript 类型检查、Vite 生产构建与
`git diff --check` 通过。构建资源为 `index-9-ACoybn.js`。

最初扩大运行旧修复测试集时出现大量基于旧调用序号/旧工作区替身的失败，已停止该次运行。
主干对照也复现了原修复主流程及旧审查读取用例失败。这里只修正与本次整合直接相关的夹具，
不宣称全项目测试通过。最终上述 102 项均通过；未调用真实模型、Docker 或发布目标仓库产物。

## review记录

由当前执行者完成代码自审，无独立审查代理或人工批准。逐项检查：

1. 审查拒绝不能被主干的后端交接绕过，必须新候选、重新测试并重读当前证据。
2. 每轮审查历史和验证历史分别保留，当前批准不能授权后续候选。
3. 主干的误报关闭、模型变更授权、旧交接恢复与 PR 的旧审查拒绝恢复均保留。
4. 暂停、依据陈旧、重复补丁、越权路径和提前交接仍拒绝执行；针对性回归通过。
5. 前端字段与后端恢复资格一致，两种恢复提示均保留；类型检查和构建通过。

本次冲突处理自审通过。只更新原 PR，不合并主干、不部署工作台、不操作业务数据库。
