# PR #73 冲突处理与 review 记录

日期：2026-10-28。关联 Issue #76，原功能 Issue #71，沿用 PR #73。

## 概述

从最新默认主干 `b6a9214b06877109af81f83a9b7c934aecf0965e` 创建
`feature/pr73-conflicts-76`，整合原 PR 头提交
`fdfeb1dff2e89b33fcd922aef30a40b3b4035675`。保留双方历史，更新原 PR 时使用普通快进推送。

## 变更内容

- 合并分析请求入口：保留主干的持久化额度预占，并传入 PR 的定向纠正指令、数据及阶段请求时限。
- 保留工具参数、必读缺失、交接、局部证据与范围关联纠正，删除旧版循环外的重复交接/必读校验。
- 同时保留报告具体诊断、范围完整性要求和安全事件容量，避免合并后丢失报告诊断。
- 修复模型保留主干的后端真实 SDK 交接/证据读取及 PR 的 Langfuse hooks。
- 更新旧上限、旧事件结构和旧证据协议的测试夹具，并增加 SQLite 持久化计数回归。

## 设计方案/说明

所有实际供应商调用仍经过 `StageModel.request`。每次请求前同步共享纠正计数、
预占持久化请求额度，再按单请求、整条链剩余时间和交接阶段剩余时间的最小值等待。
局部纠正不能直接调用 delegate 而跳过持久化计数。

保留主干最新 100000 tokens 单次硬上限、32 次累计请求、三次续办和 1800 秒总期限。
工具参数、必读/交接纠正仍共享原有限额度；实际 SDK 工具执行、完整报告、精确证据及
范围持久化门禁保留。报告诊断和工具/供应商结构诊断均使用原安全字段。

## 验证/验收

最终针对性离线回归：245 项通过，88.85 秒。

```powershell
python -m pytest tests/test_analysis_tool_arguments.py tests/test_analysis_handoff_recovery.py tests/test_analysis_report_recovery.py tests/test_analysis_auto_recovery.py tests/test_analysis_prerequisites.py tests/test_repository_analysis.py tests/test_historical_review.py -q --tb=short
```

新增回归确认原工具错误与纠正响应共消耗两次请求，纠正次数实际写入 SQLite，
旧/空计数不能覆盖已使用额度。其他回归覆盖检查点恢复、真实工具调用、交接拒绝、
必读门禁、完整范围/历史复核与证据拒绝。

Ruff 全后端/测试检查、Python compileall、TypeScript 类型检查、Vite 生产构建与
`git diff --check` 通过。构建资源为 `index-WoxXKu5f.js`。
未执行全项目测试、真实模型或 Docker，也没有重试工作台旧分析任务。

## review记录

由当前执行者完成代码自审，无独立审查代理或人工批准。逐项检查：

1. 纠正输入、schema 与原合法字段保留协议传递给实际请求，不能被主干的旧入口丢弃。
2. 每次纠正请求计入持久化额度，恢复后不重置已消耗计数；新增回归通过。
3. 主干报告反馈、扩大后的硬上限及持久化恢复仍有效，保留最新主干功能。
4. 不重复执行旧循环外的必读/交接检查；拒绝、错误工具、越界和无效证据继续阻断。
5. Langfuse hooks 与任务绑定保留，敏感内容仍不进入安全诊断事件。

本次冲突处理自审通过。只更新原 PR，不合并主干、不部署工作台、不操作业务数据库。
