# 第六阶段联合验收与启用（#83）

更新：2026-10-29（Asia/Shanghai）。复用现有中文 Issue #83；已搜索开放和关闭事项，未创建重复 Issue。分支 `feature/central-acceptance-83` 从最新实际默认分支 `codex/initial-upload` 的 `599de39` 创建。该提交是 PR #93 于 2026-10-29 09:26:03（上海时间）的合并结果。原工作区未提交内容未改动。

## 当前结论

本分支已完成隔离联合验收及跨版本来源验证修复。固定 16 个定向用例全部通过，25.27 秒；Ruff、代码及文档独立 review 通过。没有运行全量回归，没有修改前端，因此未重复构建或浏览器测试。实现提交 `0f92ff6`，已提交中文 PR #94，关联 #83、父 #77；未合并、未部署。

隔离范围使用真实 SQLite、SDK runner、分析/协调/维护/发布适配器和临时本地 Git 仓库；模型输出、GitHub HTTP、Docker/发布运行时、迁移探测由替身提供。发布后的分析确实读取隔离仓库已审查补丁的行为，再登记两个范围、修复一次提前交接并通过原流程验证，不是直接设置任务为成功。

**结论不等于生产完整目标已达成。** 本轮未读取或修改正式库，未调用真实模型、向真实维护仓库发布测试内容、执行真实远程合并或部署。正式环境的版本、开关与授权现状均未核验；不能把新库默认值当作正式库实际状态。父 Issue #77 保持开放，OpenSpec 不自动归档。

## 复现与证据

在具有开发依赖的解释器中，从本分支运行：

```powershell
python scripts/validate_central.py
```

脚本只运行固定的 16 个节点，不扫描全量测试、不读取工作台模型凭据或正式数据库；输出到忽略目录 `data/central-acceptance/<UTC时间>/pytest.xml` 和 `result.json`。任何失败、跳过或数量不符都会返回非零状态，不能用部分成功宣布通过。

本次原始结果为 `data/central-acceptance/20261029T014831121431Z/`，便携结果快照见 [JSON 记录](central-acceptance-83-result.json)。快照明确记录基础提交、工作区有修改、测试范围、每个节点结果及耗时，不冒充已提交代码或生产遥测。

新增一个联合场景，其余 15 个复用前阶段已有用例；不是补写一轮旧单元测试。联合场景的七段轨迹保存在 JUnit properties 和 JSON 中：来源故障且无部分登记、协调明确受阻、Issue/分支/审查反馈/PR、重开数据库及重放、独立授权合并与未知回执核对、授权部署及原任务唯一排队、原步骤完成后故障恢复。

## 规格验收矩阵

表中“本轮通过”均指上述离线运行，不代表真实供应商或正式服务。完整节点名称在脚本与 JSON 中，不以表格简称替换可执行节点。

| 规格 / 场景 | 测试或演练 | 证据与结果 |
| --- | --- | --- |
| workflow-recovery：可恢复超时、原证据续办 | `test_analysis_auto_recovery` 的有效检查点续办 | 本轮通过；复用已有证据，仅追加一次 reviewer 请求，累计额度不清零 |
| system-fault-records / workflow-recovery：非法批次、双登记 | 新增 `test_central_acceptance` | 本轮通过；第二条路径非法时两个条目均未落库；发布修复后两条合法登记均保留 |
| workflow-recovery：参数、交接纠正 | 新增联合场景与取消后迟到报告用例 | 本轮通过；非法路径确实拒绝，原生 handoff 实际执行，提前文本不能代替独立审查；取消不能提交迟到报告 |
| system-maintenance：审查拒绝后再验证 | 新增联合场景；`test_repair_review_feedback` 的测试/审查共享纠正上限 | 本轮通过；审查 False→True，验证失败候选不能发布；原修复反馈上限保持 |
| system-fault-records：验收失败不是自身缺陷 | `test_system_faults` 的真实验收执行结果分类 | 本轮通过；保存 executed/非零退出事实，分类 target_test，不创建系统修复；执行结果由隔离事件夹具提供，并非本轮真实 Docker |
| workflow-recovery：验收协议恢复仍不得伪造通过 | `test_acceptance_recovery` 的 SDK wire 截断恢复 | 本轮通过；真实 SDK/HTTP 替身请求有界恢复，最终 met=False 保留，不把协议恢复当业务通过 |
| workflow-recovery：两个恢复器竞争 | `test_two_connections_compete_in_business_transaction` | 本轮通过；两个 SQLite 连接仅一个动作获得执行权 |
| workflow-recovery：处理中断、未知回执 | `test_crashed_repair_settles_unknown_receipt_then_resumes` | 本轮通过；旧动作记为 interrupted/unknown，再按既有依据续办，不清零预算 |
| system-fault-records：重放、重启 | 新增联合场景重新初始化 Store、重复采集/领取 | 本轮通过；维护任务、Issue、PR 不增加；根预算持续存在 |
| system-maintenance / workbench-release：远程成功但响应丢失 | 新增联合场景的 PR POST、merge PUT 丢失回执 | 本轮通过；先核对远程事实，Issue/PR/merge 各一次，不盲目重发副作用 |
| system-coordination：所有已授权路由不可达 | `test_model_outage_without_backup_is_bounded_and_local_ledger_survives` | 本轮通过；协调尝试有界且最终 blocked，无隐式备用路由/递归任务，台账仍可用；不保证外部故障可修复 |
| workflow-recovery / system-coordination：取消、撤权 | 取消后迟到报告；诊断中禁用；发布原适配器授权用例 | 本轮通过；不提交已取消报告，迟到用量仍计账；撤回发布授权阻止来源执行 |
| workflow-recovery：执行/模型依据改变 | 新执行使原分析过期；已排队恢复模型绑定改变 | 本轮通过；原检查点/动作不能在改变的依据下继续；源码 revision 严格匹配为继承门禁，本轮未单独穷举源码变化 |
| workflow-recovery：请求额度耗尽 | `test_source_gate_stops_before_model_invocation[budget]`；发布原适配器预算检查 | 本轮通过；模型调用之前停止，发布不增加原任务额度 |
| system-maintenance / workbench-release：完整维护与发布链 | 新增联合场景 | 本轮通过；中文 Issue 在先、feature/、验证/独立 review 后中文 PR、精确 HEAD 与清单分别授权、原任务成功后才 resolved；HTTP/运行时均隔离 |
| workbench-release：回滚、升级与数据保护 | `test_rollback_keeps_new_writes_and_interrupted_release_can_reconcile` | 本轮通过；健康失败回切旧代码，保留新数据；收尾重放幂等；schema 改变时拒绝旧版回滚，不还原旧库覆盖新写入 |
| system-fault-records：不兼容来源不得解决故障 | 原修复不同模型验证；新增联合场景跨版本凭据反例 | 本轮通过；不同模型仍不解决；错误代码版本/任务、旧尝试、迟到旧证据和撤权均不可跨版本通过；缺少对应 deployed/凭据时拒绝由静态门禁核对，不计作本轮运行反例 |
| system-coordination：逐阶段启用及退回仅记录 | 新增联合场景的独立规则/协调/维护开关；诊断中禁用用例 | 本轮通过；新库默认规则/协调/维护关闭，夹具显式开启后再关闭，停止新领取；已用预算及迟到用量保留 |

## 前阶段证据核对

| 阶段 | 实现/合并 | 已有验证及 review，未在本轮全量重跑 |
| --- | --- | --- |
| 1 / #78 | PR #84 已合并 | [记录](system-fault-records-78-validation.md)；历史 1024 passed / 35 failed / 12 skipped 的基线不是绿色，不能抹除。既有失败后由 #88 / PR #89 单独修正 |
| 2 / #79 | PR #90，`367baff` | [记录](workflow-recovery-79-validation.md)；1088 passed、12 opt-in 跳过，浏览器/构建/Ruff/review 通过；该跳过不计作本轮已验证 |
| 3 / #80 | PR #91，`74b0918` | [记录](system-coordinator-80-validation.md)；定向协调/原适配器、界面/构建/Ruff/review 已有结果 |
| 4 / #81 | PR #92，`172c5a8` | [记录](system-maintenance-81-validation.md)；13 后端、1 界面冒烟、构建/Ruff/review 已通过 |
| 5 / #82 | PR #93，`599de39` | [记录](workbench-release-82-validation.md)；8 后端、1 界面冒烟、构建/Ruff/review 和真实本机进程发布/监护/回滚演练已通过 |

第五阶段原生演练与本轮 HTTP/运行时替身是两种独立证据；没有把它们写成“本轮已经真实部署 GitHub 合并后的 RepoWatch”。本轮新增修复不改 schema；已有库重新初始化与回滚保护复用上述实际用例。

## 跨版本验证契约

### 1. 触发范围

旧故障记录绑定旧工作台源码摘要；发布修复必然可能改变摘要。直接继续要求摘要相等会使原步骤成功后仍受阻；直接删掉版本门禁则会错误解决无关故障。

### 2. 签名与存储

`Releases.authorize_resume(db, source_id)` 在原 retry 的同一事务内，将 `release_resumptions` 从 dispatching 改为 verifying_original，并插入既有 `release_events` 表，无新表/迁移/API。`verifies_released_source(db, occurrence, event) -> bool` 只供故障消费者使用。

### 3. 数据契约

事件 ID 为 `resume:<清单摘要>`，kind 为 `source_resume_authorized`；details 精确包含 `occurrence_id`、`flow`、`task_id`、新运行进程的 `workbench_version`，created 为本次合法续办时间。同一次 retry 事务内的校验或排队失败时，事务整体回滚，不保留凭空授权的凭据；成功排队后实际执行失败则保留凭据与失败历史，不能解决原故障。该内部凭据不是部署成功或来源成功本身。

### 4. 校验与错误

只允许 state=resolved 的真实来源事件、严格更高 attempt、凭据之后的事件时间、对应已 deployed 且 verifying_original 的维护出现项，并重新核对有效 deploy/resume 授权及当前账号版本。失败返回 False，原故障保持未恢复。外层仓库/账号/环境/源码 revision 和实际模型绑定仍必须精确匹配。

### 5. 正常与反例

同一版本仍沿用原检查；已授权新版原分析真实完成可解决对应旧故障。错误版本、错误来源任务、旧 attempt、旧时间、撤权或不同模型均不成立。没有凭据的旧版本恢复记录不会自动被新版猜测为成功。

### 6. 必须保持的断言

联合场景在部署完成、原任务仅排队时断言故障不是 resolved；只有原分析完成、批次登记与原生交接/审查真实通过、采集器收到验证事件之后，才断言故障 resolved、发布续办 completed。反例覆盖版本/来源/时间/attempt/状态/撤权；原模型不兼容用例继续通过。

### 7. 错误与正确做法

错误：部署成功就 transition(resolved)，或全局忽略 workbench_version。正确：同事务保存精确发布续办凭据，只为对应原来源的真实成功事件关联旧版本，继续保留其他所有依据校验。

## 效果指标

本次联合场景涉及 1 个来源故障，原步骤验证恢复 1 个；这只是确定性夹具结果，不是“生产恢复率 100%”。源码修改使缺陷样例的实际行为改变，原分析续办前后根请求计数为 **3→17**，额外 **14**；约 **5.108 秒**，不是生产延迟承诺。

Issue POST、PR POST、merge PUT 各 **1**。夹具显式权限操作 **5** 类：规则、协调、维护、精确 HEAD 合并、精确清单部署；远程响应丢失后的显式核对 **2** 次。不能把该联合链路称为无需人工介入。tokens_reported=0 仅为替代模型未完整报告用量，`token_usage_complete=false`，费用为 null，不能解释为真实调用免费或零令牌。真实模型调用和 GitHub 业务写入均为 0。

## 分步启用与回退

以下为所有者操作顺序，不是本次已经在正式环境执行。每一步先保存并核对当前版本、账号、剩余额度及上一有效配置；只有该步已有明确授权才操作，不能因第六阶段验收通过而批量开启。

| 步骤 | 进入条件 / 操作 | 关闭或回退 |
| --- | --- | --- |
| 仅记录 | 登录实际工作台，按[阶段一](system-fault-records-78.md)核对采集健康；历史补录单独决定 | 可关闭采集；关闭不删除既有历史，不触发旧任务 |
| 规则恢复 | 按[阶段二](workflow-recovery-79.md)确认来源依据、原根预算和恢复开关版本，再显式开启 | 关闭后不领取新自动动作；不清零预算或取消原业务，未知副作用先核对 |
| 中央协调 | 按[阶段三](system-coordinator-80.md)独立配置协调模型/额度并授权；不覆盖五个业务模型 | 关闭协调，不自动改备用模型或扩大额度；已有执行安全收尾并计账 |
| 自身维护 | 按[阶段四](system-maintenance-81.md)核对 Gwex323/RepoWatch、账号版本、Python 补丁边界，单独授权 | 关闭维护，保留 Issue/PR/拒绝记录；不删除远程结果掩盖失败 |
| 辅助合并 | 所有者可自行在 GitHub 合并；使用 CLI 时仅授权已审查的精确 HEAD，仍须 GitHub 规则通过 | revoke 对应 merge target；不能用旧授权批准新提交 |
| 部署与原任务续办 | 按[阶段五](workbench-release-82.md)核对已合并提交、干净 checkout、原库/备份、资产、监控配置和固定端口；显式授权清单，resume-original 另行明确选择 | revoke 对应 deploy target，先 reconcile 未知发布回执；需回滚时仅回切已知兼容代码，不盲目还原数据库备份 |

正式启用后应记录：实际清单/代码版本、权限绑定、观察时间窗、各类故障总数及被验证恢复数、失败原因、恢复耗时分布、根请求/令牌/已知费用、人工授权和核对次数。没有生产观测样本时填“未核验”，不得复制本地夹具数字。超时或全部模型不可达可以正确受阻，不承诺一定可自动修复。

## 未核验与交付边界

- 本轮后端采用隔离替身；真实付费模型/备用路由、真实维护 GitHub 合并与目标版本发布未执行。
- 原生 Web/监护/固定端口演练引用第五阶段既有证据，本轮没有重新运行，不宣称覆盖本次所有跨流程组合。
- 发布后原分析的跨版本恢复已联合验证；修复/验收原适配器的既有预算、授权及证据门禁仍有效，但未分别进行真实原生部署后的完整联调。
- 本轮动态检查执行依据和模型变化；源码字节/revision 变更门禁沿用前阶段实现和已有验证，未另行扩展源码变更组合。本矩阵不把继承的静态检查计为新的动态用例。
- 未验证正式 Langfuse 远端连通、Linux 或所有语言/供应商；Python 自身补丁边界未扩大，不能把这些未支持能力勾选为完成。
- 本轮完成的是定义内的隔离实现与验收交付，不是正式工作台已部署/已自动启用。父 #77 不自动关闭，正式整体目标的最终关闭须另行核对这些边界及实际运行证据。

## Review 与交付

复用 `coordinator_review` 完成只读独立代码及文档 review，通过，未发现 P1/P2 阻塞；没有为 review 扩展测试矩阵。文档复核已收窄 retry 回滚范围、静态门禁与动态反例、执行变化与源码变更三处表述。中文 PR #94 已提交，尚未合并或部署。OpenSpec 51/51 表示定义内隔离实施与交付已有对应证据，不等于上述正式环境及跨平台未核验事项完成；父 #77 保持开放，变更不自动归档。
