# 独立审查必须先读取真实验证证据（Issue #53）

## 触发范围

修复已通过测试且完成原生 handoff，但审查模型先返回最终结论。根因是仅用提示词要求
读取工具，同时又要求立即输出 JSON；此前合成模型始终主动读取，遗漏了真实供应商的
提前结论行为。#42 的测试失败反馈不覆盖这种审查协议错误。

## 接口

`RepairStageModel.get_response()` 在 `verification_read=false` 时只提供
`read_repair_verification`，设置对应 `tool_choice`，暂不提供最终输出 schema。
真实工具执行后才允许最终审查。最终通过仍要求原生交接、通过的验证和证据读取。

## 数据契约

`GET /api/repairs/{id}` 增加 `review_evidence_recovery_available`。持久化 context 使用
`review_evidence_policy=required_review_evidence_v1` 和一次性的
`review_evidence_recovery_used`。没有数据库结构变更。

提前结论被丢弃并记录 `review_evidence_retry`，同一运行最多纠正一次；共用已有超时、
输出预算及暂停/取消检查，不提供额外的无限模型轮次。不得记录原始模型结论。

## 校验和错误

| 情况 | 行为 |
| --- | --- |
| 未读取便返回结论或调用其他工具 | 丢弃整份输出，有界要求调用证据工具 |
| 纠正后仍不读取 | `repair_review_evidence_required`，不得发布 |
| 真实原生交接缺失 | 保留 `repair_handoff_incomplete` |
| 暂停、超时、测试不通过或证据失效 | 原门禁继续阻止后续操作 |

## 正常与异常恢复

旧失败任务只有在次数恰好耗尽、旧策略、审查阶段、已通过验证、存在补丁和源码摘要、
没有审查及发布结果，并且最后一次尝试记录了唯一原生交接且没有证据读取工具事件时，
才提供一次显式恢复。当前证据有效、GitHub 身份不变也是重试条件。

恢复保留历史和补丁，重新校验源码摘要、重建候选并重跑测试，然后进行新的原生交接和
独立审查。不得重新生成源码或复用未经重新验证的通过状态。部署不自动重排失败任务。

## 测试断言

`tests/test_review_evidence.py` 使用真实 SDK 与合成模型，覆盖提前结论后实际读取、
持续拒绝读取、审查否决、暂停、一次恢复、历史保留和资格排除。
`tests/e2e/repair.spec.ts` 校验页面原因、纠正事件及恢复按钮。

## 禁止与正确做法

禁止删除 `review_read` 门禁，或将模型自称“已读取”视作工具执行。正确做法是在预算内
纠正工具调用，只有 SDK 实际执行证据工具并取得可信差异与日志后才接收独立审查结论。
