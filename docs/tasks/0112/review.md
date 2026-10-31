# Issue #112 静态审查与验证记录

- 日期：2026-10-31（Asia/Shanghai）。
- Issue：[Gwex323/RepoWatch#112](https://github.com/Gwex323/RepoWatch/issues/112)。
- 分支：`feature/scope-review-recovery-112`。
- 基线：`origin/codex/initial-upload` 的 `5c25840`。
- 状态：首轮独立 flash 审查发现已由 v4pro 修正，等待 flash 复审；PR 尚未创建。

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

首轮 flash 审查后补强：

- 无进展检测从“最后两条 failure 必须都有指纹”改为检查最近 64 条 failure 中最近两条
  有效指纹，可跳过 provider 失败、损坏事件或无指纹失败。
- 失败指纹始终尝试由报告候选生成；范围问题使用排序后的固定问题标签，范围审查使用稳定
  排序结果，不因 `required_keys` 顺序或理由措辞变化而误判为新进展。
- 范围纠正反馈只发送前 6 条固定标签，并显式携带 `total` 与 `truncated`，避免 4096
  字符事件截断破坏可操作性。
- 映射错误路径先应用统一范围语义；通用纠正无论此前是否存在 `report_correction` 都清除
  陈旧私有纠正检查点。

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

首轮 flash 审查结果：`approved=false`，6 项发现。审查文件为
`.codex-backups/flash-review-scope-112.json`，用量 prompt 9667、completion 1537、
total 11204。

针对首轮发现，真实 v4pro 修正方案生成记录：

- 脚本：`.codex-backups/v4pro_fix_scope_112.py`
- 结果：`.codex-backups/v4pro-fix-scope-112.json`
- 原始响应：`.codex-backups/v4pro-fix-scope-112-raw.json`
- 用量：prompt 22799、completion 3652、total 26451

主路径采纳了跨重启指纹、无序范围签名、旧检查点清理和映射语义优先等修正；未采纳
v4pro 关于扩大两个小型映射事件上限的建议，并修正了其测试草案中的调用次数问题。

## 已通过验证

- `tests/test_analysis_tool_arguments.py`：38 passed。
- `tests/test_analysis_auto_recovery.py`：8 passed。
- `tests/test_analysis_report_recovery.py`：29 passed、1 failed；失败项为
  `test_resume_still_rejects_fabricated_evidence_and_persists_no_findings`，实际 4 次请求、
  预期 3 次。该失败已在未修改的 `5c25840` 基线复核。
- 新增 GUI 排除项测试确认缺少 `client` 证据的映射在审查阶段被拒绝，具备 `client`
  证据的映射通过。
- 新增重复语义测试确认模型只改理由时仍只允许一次纠正，并在同一语义再次出现时停止。
- 新增测试确认无关失败事件不会破坏无进展指纹，且范围反馈超过 6 条时明确标记截断。
- 新增测试确认映射错误与范围语义错误同时出现时，范围语义优先且不会绕过校验。
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

首轮独立审查未通过，相关发现已修正并完成定向验证。按仓库规定，创建 PR 前仍需让
`deepseek-flash` 对修正后的提交重新执行独立审查；复审结果将在本节追加。
