# 取消人工复现确认与自动修复资格（#97）

## 行为

自动修复开启时，有效最终分析报告保留的发现直接成为修复候选并入队，无需点击“复现成功”。自动修复关闭时只保存候选，显示等待开启；开启后补采。模型、环境、仓库暂停、版本和账号等现有条件继续控制执行。关闭开关会停止领取，运行中任务在受控边界检查后停止。

复现辅助角色继续整理步骤，回归测试继续证明修复前失败、修复后通过及完整测试结果，独立修复审查通过后才允许发布。取消人工确认不改变合并或部署授权。

## 状态与来源

`repair_candidate` 表示分析结果允许尝试修复，不表示本人复现或测试已证明缺陷。任务进度单独展示排队、修复中、验证及发布状态。

`finding_repair_origins` 使用 finding ID 唯一关联分析 ID、源码 revision、配置 fingerprint、执行 trigger_key、发现摘要、策略版本及证据引用。只有服务端已验证的结果写入来源。最终审查快照及摘要在来源中保持不可变，后续范围复审不覆盖原依据；API 和模型 context 仅返回证据引用，不重复传送整个快照。来源类型包括最终分析 `analysis_review`、当前版本复查 `current_recheck` 和验收续办 `acceptance_followup`。真实旧 owner 确认可读取为 `legacy_owner`，不会为历史 pending 伪造此来源。

来源是修复资格的一部分；入队、领取、重试、恢复、发布和验收还会核对当前依据、账号、仓库暂停、取消与预算。修复 context 的 `repair_origin` 传递事实，不能代替数据库中的实时资格。旧 `confirmation` 仅用于读取真实历史。

发现可标记未能复现、误报或已解决；原因要求及版本检查沿用统一处置。处置立即使该记录失去后续执行资格，已排队任务取消，运行任务在下一受控步骤停止。若远程请求已经发出，可能仍产生远程结果；必须保存真实回执并核对，不能声称已撤回或重复创建。

## API 与界面兼容

- `/feedback` 的 `decision=confirmed` 返回 409、`manual_confirmation_retired` 和迁移提示，无状态写入。`personally_reproduced` 可由旧客户端提交，但不能创造新资格。
- 旧 `not_reproduced` 写入委托统一处置；新客户端使用 `/disposition`。认证、CSRF、数据作用域、版本及原因校验继续生效。
- `/continue` 依据当前版本复查、源码、Issue 来源、范围及独立范围审查登记后继候选，不再要求旧问题为本人确认。
- 分析响应提供 `repair_origin` 和 `repairs_enabled`；问题页展示等待条件和处置操作。旧 reproduction 子页标识作为兼容路由保留，标题为“问题与修复”。
- dashboard 的 `counts.waiting` 与 `repair_candidates` 表示尚无关联修复任务的候选数；历史待复现记录有单独状态。`analysis-notifications.waiting` 保留旧历史计数，新候选使用 `repair_candidates`。记忆指标中 `confirmed_findings` 仅计真实历史本人确认，新增 `repair_candidates` 不混算。

## 历史复查与验收

历史复查纳入同仓库未关闭候选，保留数量上限、裁剪与覆盖限制。历史复查提及不等于当前可执行资格；须有当前有效报告发现，或经当前源码和范围验证的接续记录。

验收可以读取有效分析来源修复的补丁和验证证据。原问题在真实验收中失败时，可在原上限和共享预算内登记续办来源，关联根发现、验收运行和失败项；与原问题无关联的新缺陷需进入正常分析，不伪造本人确认。关闭/解决记录可保留历史验证供查看，不能再启动修复。

## 升级和回退

新增表为加法初始化，可重复执行，不修改原 enabled、反馈、任务、预算或远程回执。新安装仍默认关闭自动修复；已有启用状态沿用。

历史 `waiting_reproduction` 不因升级入队，界面提示重新分析当前版本；有效新候选按正常流程推进。真实旧确认可继续支持原任务。关闭、取消、已失败或耗尽预算的任务不会因补采自动重启。

回退前关闭自动动作并排空或停止活动任务，保留新增表和新候选。旧版本不认识 `repair_candidate`，不得把它转成 confirmed 或伪造 owner 反馈。存在新写入时不覆盖整库备份，不清除预算和远程回执。

## 开发依据与状态

GitHub 写入身份：`Gwex323`；中文 Issue：#97。开发分支为 `feature/remove-manual-reproduction-97`，基于实际远程默认分支 `codex/initial-upload` 的 `f8e91ed45a463d3d056856da57d59cfbbef46a5a`，隔离工作区保留原目录内容。

已核对相关开放与关闭 Issue：#3 的目标为“人工复现之后修复”，#12 仅针对已有 GitHub Issue 的验证，#67 为页面拆分，#77 为自身运行故障调度；本次改变系统新发现的修复资格，采用独立 #97。

分支实现、相关离线验证及代码差异自审已完成；review 记录见 [验证与审查](tasks/0097/review.md)。PR 交付、GitHub 合并和运行服务部署分别记录，尚未合并或部署。


## 规格与验收证据映射

以下均为隔离 SQLite、确定性 SDK 模型、运行时和 GitHub 发布替身的离线验证；不代表真实模型质量、目标仓库 Docker 或生产部署验收。

| 契约 | 验收证据 |
| --- | --- |
| 有效最终报告产生候选且同事务落库 | `test_analysis_automatically_queues_and_publishes_without_owner_feedback`、`test_final_report_and_candidate_origin_roll_back_together`，以及分析模块无发现/无效报告回归 |
| 来源与执行资格不可由客户端声称替代 | `test_candidate_requires_actual_current_provenance`；修复模块的账号、配置、源码和当前执行依据检查 |
| 保留开关、等待与幂等 | `test_analysis_candidates_backfill_once_after_enabling_without_confirmation`、并发补采测试、`test_bounded_backfill_does_not_starve_valid_candidates` |
| 测试与独立修复审查继续控制发布 | `tests/test_repairs.py`、`tests/test_review_evidence.py`、`tests/test_github_contributions.py`、`tests/test_github_publication_failures.py` |
| 用户处置阻止后续动作，在途请求保留回执 | `test_disposition_blocks_queued_and_running_candidate`、`test_disposition_during_remote_request_keeps_actual_receipt`、统一处置模块 |
| 历史 pending 不升级执行；真实旧确认可读 | `test_upgrade_does_not_activate_pending_or_closed_history`、`test_real_legacy_confirmation_remains_usable` |
| 当前版本复查接续需源码与范围证据 | `test_current_recheck_continues_old_pending_without_forging_confirmation` 及四项证据失效用例；历史复查模块覆盖隔离、裁剪、数量上限 |
| 验收读取候选、原范围失败有界续办 | `test_published_candidate_acceptance_creates_bounded_evidence_followup`（regression/integration）；验收恢复模块继续校验持久化额度 |
| 旧正向 API 明确退役，负向使用统一处置 | API 认证/CSRF/开关用例、`test_manual_confirmation_is_retired_without_writes`、`tests/test_finding_disposition.py` |
| 页面、过滤与指标使用候选语义 | `test_metrics_separate_candidates_from_real_legacy_confirmations`；分析与导航浏览器用例 |
| 加法迁移、重复初始化与回退 | `test_old_schema_upgrade_preserves_settings_budgets_feedback_and_receipts`、事务回滚用例；下述旧资格函数回退演练 |
| 验收接续不能扩大原范围或混入新缺陷 | `_followup` 继续要求原 repair_id 和 scope 映射，根问题最多两次；新来源递归核对原发现与持久化验收项/结果/审查摘要 |

## 兼容与回退演练记录

2026-10-29，仅只读核对原工作台主库：5 个环境、6 个发现和 6 个修复任务；自动修复 enabled=true、version=6；尚无新增来源表。本轮没有在该库运行迁移或启动新版本服务。

旧库夹具保留真实结构，模拟没有新增来源表的库；分别在 enabled=false/true 下重复初始化两次，逐行比较反馈、修复任务、开关、事件、共享额度、动作和用量记录，结果一致，已发布远程回执保留。新安装默认关闭不变。

另在临时库中从 `f8e91ed45a463d3d056856da57d59cfbbef46a5a` 提取并实际执行旧版 `Repairs.current`：新版本候选在当前资格判断下有效，旧判断拒绝该候选；带真实历史 owner 反馈的 confirmed 对照仍可读。执行前后任务行完全一致，1 项通过。演练脚本位于忽略目录 `data/rollback97/`，不随产品发布。该演练验证旧资格判断，不代表生产进程已切换或真实部署回滚。
