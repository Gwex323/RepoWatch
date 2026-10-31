# Issue #112 静态审查与验证记录

- 日期：2026-10-31（Asia/Shanghai）。
- Issue：[Gwex323/RepoWatch#112](https://github.com/Gwex323/RepoWatch/issues/112)。
- 分支：`feature/scope-review-recovery-112`。
- 基线：`origin/codex/initial-upload` 的 `5c25840`。
- 状态：实现与静态自审完成；独立模型审查待执行，PR 尚未创建。

## 根因

真实分析 `analysis_0cb02e0b1c7148ee` 在独立审查阶段反复返回
`scope_review_incomplete`。审查契约只检查范围项、排除项集合和映射引用，没有检查
`required` 排除项的目标范围项是否具备规定的证据类型；该语义只在运行结束后的
`save_scope_review()` 中校验。因此候选能通过审查并写入恢复检查点，却必然在保存时
失败。恢复机制随后重放同一检查点和同一候选，没有有限停止条件。

现场达到 `requests=45`、`resumptions=7`。典型无效映射为
`excluded:tests/test_display_names.py -> trae_cache_adapter_safety`：当前执行限制包含
`windows_native`，要求目标项具备 `client` 证据，但目标项只有
`integration, regression`。

## 实现

1. 在 `scope_models.py` 新增唯一的 `scope_review_issues()`，集中生成固定标签问题：
   排除项键、目标项键、所需证据类型、可用证据类型和固定原因。
2. `task_scopes.py` 的保存门禁复用同一函数，删除重复语义，保持原有 fail-closed 行为。
3. `repository_analysis.py` 在审查阶段调用同一语义检查，并输出 `scope_review_validation`
   结构化事件和中文纠正提示；无合适目标时要求改为 `unknown`，不要求伪造证据。
4. 同一 `scope_review_incomplete` 的错误语义再次出现时抛出
   `analysis_report_no_progress`；范围问题按固定语义签名，仅修改理由或措辞仍视为无进展。
5. 恢复调度遇到 `analysis_report_no_progress` 立即停止；连续两次安全指纹相同的失败也以
   `stop_reason=no_progress` 停止。指纹只含错误码、检查点摘要和范围审查摘要，不保存正文。
6. 通用最终报告纠正覆盖旧的私有纠正检查点时，先清除陈旧检查点，确保内存状态与恢复状态
   一致。
7. `scope_review_validation` 与 `failure` 事件上限提高到 4096 字符，避免结构化诊断被截断。
8. 增加 GUI/Windows 证据类型、重复无效语义、恢复停止、事件完整持久化和陈旧纠正清理测试。

## 模型路由记录

真实 `deepseek-v4-pro` 仅用于方案分析，未让其直接提交未经审查的代码：

- 脚本：`.codex-backups/v4pro_scope_review_112.py`
- 结果：`.codex-backups/v4pro-scope-review-112.json`
- 原始响应：`.codex-backups/v4pro-scope-review-112-raw.json`
- 用量：prompt 14731、completion 4563、total 19294

v4pro 给出了恢复无进展检测方向，但建议中包含不存在的 `last_fingerprint` 字段及缺少迁移的
检查点读取。主执行路径未照搬该方案，改用现有 `analysis_events` 的安全摘要查询，并复用
现有检查点结构。因此本记录只声明“v4pro 生成方案、主路径修正落地”，不声明 v4pro
独立完成全部代码。

## 已通过验证

- `tests/test_analysis_tool_arguments.py`：32 passed。
- `tests/test_analysis_auto_recovery.py`：7 passed。
- `tests/test_analysis_report_recovery.py`：29 passed、1 failed；失败项为
  `test_resume_still_rejects_fabricated_evidence_and_persists_no_findings`，实际 4 次请求、
  预期 3 次。该失败已在未修改的 `5c25840` 基线复核。
- 新增 GUI 排除项测试确认缺少 `client` 证据的映射在审查阶段被拒绝，具备 `client`
  证据的映射通过。
- 新增重复语义测试确认模型只改理由时仍只允许一次纠正，并在同一语义再次出现时停止。
- 检查点清理测试确认新的通用纠正会清除旧私有映射检查点。
- `ruff check repowatch tests`：通过。
- 修改模块 `py_compile`：通过。
- `git diff --check`：通过。

## 基线失败与未完成验证

- `tests/test_repository_analysis.py` 两个失败均为既有测试漂移：
  `StageModel(..., force_closing=True)` 使用了当前基线构造函数不存在的参数；已在 detached
  基线工作树复现相同 `TypeError`。
- 该文件运行到第 100 项 `test_native_source_tools_handoffs_and_evidence_validation` 时超过
  120 秒无输出，已中止；不把该文件的全量运行表述为通过。
- 尚未用真实分析重跑 `analysis_0cb02e0b1c7148ee`，尚未重启 `8765` 生产工作台，未创建
  或合并 PR，未部署。

## 审查结论

本轮静态自审通过，未发现待修正的阻断问题。按仓库规定，创建 PR 前仍需对同一 diff 执行
独立 `deepseek-flash` 审查；审查结果将在本节追加。
