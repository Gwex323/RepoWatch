# Issue #112 静态审查与验证记录

- 日期：2026-10-31（Asia/Shanghai）。
- Issue：[Gwex323/RepoWatch#112](https://github.com/Gwex323/RepoWatch/issues/112)。
- 分支：`feature/scope-review-recovery-112`。
- 基线：`origin/codex/initial-upload` 的 `5c25840`。
- 状态：五轮独立 flash 审查发现均已核验并补强，等待 flash 第六轮复审；PR 尚未创建。

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
7. `scope_review_validation` 事件上限提高到 4096 字符，避免结构化诊断被截断；
   `failure` 事件单独限制为 1024 字符，并由 `failure_event_payload()` 保持完整 JSON。
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

第二轮 flash 审查后补强：

- 失败指纹不再读取或摘要整个候选报告。范围审查使用只含整数、布尔值、类型名及字符串
  SHA-256 的无序结构；范围审查缺失或类型错误时只记录已知字段的类型和计数。
- 无进展指纹查询改为 SQLite 直接提取最近两条有效 `progress_fingerprint`，不再依赖固定
  事件窗口，也不解析或保留候选正文。
- `failure` 事件恢复 1024 字符上限；指纹提前写入，预算信息超出 1000 字符时先移除预算，
  再降至有界标量，必要时只保留错误码、阶段和进度指纹，保持失败事件仍是完整 JSON，
  且无进展指纹不被截断。

第三轮 flash 审查后核验：

- flash 担心相同指纹会误停仍有真实读取进展的分析。核验确认失败指纹已包含审查检查点的
  `payload_digest`，检查点在新增读取、handoff 或 observations 后会变化；新增测试直接
  证明检查点从“前一次读取”变为“新增读取”时指纹不同。
- 将无序范围结构的排序键从 `repr()` 改为固定 JSON 规范化字符串，消除动态对象表示带来的
  排序歧义。
- 新增最坏长度测试，所有可选文本字段都使用超长值，确认失败事件仍为可解析 JSON、
  总体不超过 1000 字符，并保留有界进度指纹。
- 新增审查阶段与保存阶段的交叉测试：同一 GUI/Windows 范围语义在
  `scope_report_issue()` 和 `save_scope_review()` 都返回 `scope_review_incomplete`，
  证明两个阶段复用同一 fail-closed 语义。

第四轮 flash 审查后补强：

- `bounded_scope_issues()` 输出前按当前 `task_scope` 白名单检查排除项键和目标项键；
  已注册键保留可操作性，候选自行提交的未注册键改为固定 SHA-256 标识，不再把任意候选
  文本回灌给模型或写入 `scope_review_validation`。
- 提取 `report_progress_material()`，让进程内 `scope_review_attempts` 与跨重启
  `report_failure_fingerprint()` 共享同一范围语义材料构造；两处仅在持久化侧追加
  checkpoint 摘要，避免判定材料继续漂移。

第五轮 flash 审查后补强：

- 范围语义解析异常时，无进展材料只使用固定
  `invalid_scope_review_structure` 标签，不再回退到畸形候选的部分结构摘要。
- `failure_event_payload()` 在最小结构仍超限时只保留有界进度指纹，确保失败事件最终序列化
  不超过 1000 字符，且不依赖 `analysis_events.agent` 的截断。
- `repeated_no_progress()` 改为按最新安全指纹统计其全部历史出现次数，不再使用固定事件
  窗口；A-B-A 中即使 B 是另一个安全失败，只要最新 A 已重复即可停止。
- `scope_review_issues()` 对 `required_keys` 先去重再排序，避免重复目标占用 6 条反馈预算。

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

第二轮 flash 复审结果：仍为 `approved=false`，6 项发现，其中 P1 包括指纹回退材料可能
接触候选正文、回退未规范化无序结构。审查文件为
`.codex-backups/flash-review-scope-112-r2.json`，用量 prompt 13469、completion 1506、
total 14975。

第二轮 v4pro 修正方案记录：

- 脚本：`.codex-backups/v4pro_fix_scope_112.py`
- 结果：`.codex-backups/v4pro-fix-scope-112-r2.json`
- 原始响应：`.codex-backups/v4pro-fix-scope-112-r2-raw.json`
- 用量：prompt 29972、completion 2731、total 32703

主路径采纳了无正文安全指纹、无序结构、事件容量和测试补强方向。v4pro 建议扫描全部
failure 事件并从 Python 解析；主路径改为 SQLite 直接提取最近两条有效 JSON 指纹，避免
整表加载。v4pro 的 `canonical_scope_review` 草案仍会在畸形结构上抛异常，主路径改用
文本哈希和类型/计数结构替代。

第三轮 flash 复审结果：仍为 `approved=false`，5 项发现，其中 P1 包括检查点进展与失败
事件最坏长度。审查文件为 `.codex-backups/flash-review-scope-112-r3.json`，用量
prompt 16268、completion 1259、total 17527。

第三轮 v4pro 核验记录：

- 脚本：`.codex-backups/v4pro_fix_scope_112.py`
- 结果：`.codex-backups/v4pro-fix-scope-112-r3.json`
- 原始响应：`.codex-backups/v4pro-fix-scope-112-r3-raw.json`
- 用量：prompt 34954、completion 646、total 35600

v4pro 判定 5 项发现均不成立，主要依据是检查点摘要覆盖真实进展、Pydantic 强制候选结构、
固定字段排序确定且测试已覆盖持久化。主路径核验后同意“误停”风险不成立，但仍增加检查点
变化、最坏长度和审查/保存交叉测试，并把排序键改为 JSON 规范化形式，以可执行回归覆盖
审查所指风险，而不是仅以模型结论关闭发现。

第四轮 flash 复审结果：仍为 `approved=false`，6 项发现，其中 P1 包括未注册范围键可能被
反馈回显，以及进程内与持久化去重材料不一致。审查文件为
`.codex-backups/flash-review-scope-112-r4.json`，用量 prompt 18211、completion 1503、
total 19714。

第四轮 v4pro 核验记录：

- 脚本：`.codex-backups/v4pro_fix_scope_112.py`
- 结果：`.codex-backups/v4pro-fix-scope-112-r4.json`
- 原始响应：`.codex-backups/v4pro-fix-scope-112-r4-raw.json`
- 用量：prompt 38503、completion 3391、total 41894

v4pro 接受“候选键必须白名单脱敏”和“内存/持久化签名材料需统一”两项，拒绝其余四项：
通用纠正分支已清理旧检查点；真实落库失败路径的候选先经过 Pydantic；畸形报告只形成确定性
无效结构；budget 不参与恢复判定。主路径采用两项结构性修正，并将 v4pro 的哈希后键方案
改为默认 fail-closed 白名单：没有 context 时也不原样输出候选键。

第五轮 flash 复审结果：仍为 `approved=false`，6 项发现，其中 P1 包括空范围审查旁路和
异常结构指纹材料；P2 包括失败事件最终长度与 A-B-A 重复检测。审查文件为
`.codex-backups/flash-review-scope-112-r5.json`，用量 prompt 19357、completion 1319、
total 20676。

第五轮 v4pro 核验记录：

- 脚本：`.codex-backups/v4pro_fix_scope_112.py`
- 结果：`.codex-backups/v4pro-fix-scope-112-r5.json`
- 原始响应：`.codex-backups/v4pro-fix-scope-112-r5-raw.json`
- 用量：prompt 40200、completion 3870、total 44070

v4pro 接受固定异常标签、最终长度硬上限、A-B-A 重复检测和重复目标键去重；拒绝
“空 `scope_review` 会旁路”和旧纠正清理，因为前者已由 `scope_review_required` 显式拦截，
后者由通用纠正分支持续清理。主路径采用四项，但未采用 v4pro 的 100 条固定窗口，改为对
最新指纹统计全部历史出现次数，避免把窗口盲区重新引入。

## 已通过验证

- `tests/test_analysis_tool_arguments.py`：48 passed。
- `tests/test_analysis_auto_recovery.py`：10 passed。
- `tests/test_analysis_report_recovery.py`：29 passed、1 failed；失败项为
  `test_resume_still_rejects_fabricated_evidence_and_persists_no_findings`，实际 4 次请求、
  预期 3 次。该失败已在未修改的 `5c25840` 基线复核。
- 三个定向测试文件分别运行，共 87 passed、1 failed；唯一失败即上述已在基线复现的
  既有断言。
- 新增 GUI 排除项测试确认缺少 `client` 证据的映射在审查阶段被拒绝，具备 `client`
  证据的映射通过。
- 新增重复语义测试确认模型只改理由时仍只允许一次纠正，并在同一语义再次出现时停止。
- 新增测试确认无关失败事件不会破坏无进展指纹，且范围反馈超过 6 条时明确标记截断。
- 新增测试确认映射错误与范围语义错误同时出现时，范围语义优先且不会绕过校验。
- 新增测试确认畸形范围候选的指纹材料不含候选摘要、证据原文或任意私有字符串，且不同
  措辞、项目顺序不会改变无进展指纹。
- 新增测试确认失败事件超长预算会被移除以保留完整指纹和 1024 字符上限。
- 新增测试确认即使没有 `budget` 且其他诊断字段异常超长，序列化结果仍不依赖事件截断。
- 新增测试确认检查点摘要变化会改变无进展指纹，真实读取进展不会被误判为无进展。
- 新增测试确认所有失败事件文本字段极端超长时仍输出有界、可解析且保留指纹的 JSON。
- 新增交叉测试确认审查阶段与保存阶段对同一 GUI/Windows 映射都失败关闭。
- 新增测试确认已注册范围键原样保留，未注册候选键只输出固定 SHA-256 标识，反馈和事件
  都不包含候选私有字符串。
- 新增测试确认缺失 `scope_review` 明确返回 `scope_review_required`，不会被审查契约放过。
- 新增测试确认异常范围结构只产生固定无值标签，不读取畸形候选内容。
- 新增测试确认 A-B-A 安全失败序列能识别同一指纹重复，且重复目标键只生成一条反馈。
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

前五轮独立审查均未通过，相关发现已核验、修正并完成定向验证。按仓库规定，创建 PR 前仍需
让 `deepseek-flash` 对修正后的提交重新执行独立审查；复审结果将在本节追加。
