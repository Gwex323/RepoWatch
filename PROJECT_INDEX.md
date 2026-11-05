# 项目索引

RepoWatch 面向本地开发场景，规划以受控 Agent Harness 扫描 GitHub 仓库、解决 Issue、验证修复并提交和跟进 PR。用途见 [README](README.md)，接续状态见 [NOW](docs/context/NOW.md)。

本页是条件路由，不是必读清单。先选当前任务对应行，再定位目标文档的相关标题或关键词；无关链接无需打开。

## 任务路由

| 当前问题 | 定位入口及范围 |
| --- | --- |
| 确认协作、读取和写入规则 | [AGENTS](AGENTS.md)，并确认目标目录适用规则 |
| 接续工作、查当前阻塞和下一步 | [NOW](docs/context/NOW.md) |
| 了解项目用途及当前使用方式 | [README](README.md) |
| 定位现有目录、配置或拟建模块 | [MAP](docs/context/MAP.md) |
| 检查规划、准备实施或查操作前置条件 | [RUNBOOK](docs/context/RUNBOOK.md)，只读对应操作段 |
| 安装开发依赖、运行 P0 原型与检查当前实验 | [开发入口](docs/development/README.md)、[P0 报告](experiments/results/p0.md)、[P0 门槛汇总](docs/development/p0-summary.md)；供应商实验见 [E1 说明](docs/development/e1-providers.md)，对象交接见 [Git 交接说明](docs/development/git-handoff.md)，回执恢复见[恢复模拟](docs/development/recovery-simulation.md)，调度见[调度模拟](docs/development/scheduling-simulation.md)，编码资产见[编码资产评测](docs/development/embedding-evaluation.md)，字段定义见[协议说明](docs/development/protocols.md) |
| 查产品范围、自动化边界 | [需求基线](docs/requirements-baseline.md)，按运行、扫描、Issue、验证、供应商、记忆章节定位 |
| 查最终架构、对象及模块概览 | [设计基线](docs/design-baseline.md)；具体调用归属转 [实施契约](docs/implementation-contracts.md) §2 |
| 查决定及取舍理由 | [ADR 集合](docs/adr/)，先搜索标题或关键词，再读命中的 accepted 记录；未独立成 ADR 的已确认范围见需求基线 |
| 查术语含义 | [GLOSSARY](GLOSSARY.md)，定位对应条目 |
| 查状态、预算、执行权与恢复 | [实施契约](docs/implementation-contracts.md) §3、§10；流程说明见 [工作流程](docs/workflow-design.md) Q30 |
| 查已实现的业务数据端口、迁移入口与正式调度 | [业务持久化](docs/development/persistence.md)；当前接续点见 [NOW](docs/context/NOW.md)，任务与验收记录见 [tasks](openspec/changes/build-github-issue-agent-harness/tasks.md) |
| 查已实现的预算计量、单步预留和有限续期 | [累计预算](docs/development/budgets.md)；产品规则见 [实施契约](docs/implementation-contracts.md) §3 |
| 查 P1 控制台、开发启动及人工处理 | [本地控制](docs/development/local-control.md)，恢复前置见[控制与恢复](docs/development/control-recovery.md) |
| 查正式图、同步子任务及持久进展／故障验收 | [运行引擎](docs/development/runtime.md)，当前通过范围见 [NOW](docs/context/NOW.md#证据入口与时效) |
| 查导入扫描、自动补全与版本漂移 | [实施契约](docs/implementation-contracts.md) §4；[自动补全规则](docs/scan-recovery-proposal.md)；[工作流程](docs/workflow-design.md) Q28 |
| 查修复验证、发布或 PR 返修 | [实施契约](docs/implementation-contracts.md) §5、§8；[工作流程](docs/workflow-design.md) Q29 与“PR 人工评审未通过” |
| 查供应商、Agent 交接、凭证 | [接入设计](docs/integration-discussion.md) Q31、Q35；[实施契约](docs/implementation-contracts.md) §6；已实现端口及验证边界见 [P1 供应商](docs/development/providers.md) |
| 查巡检频率和同步行为 | [实施契约](docs/implementation-contracts.md) §7 |
| 查记忆内容、检索、反馈和纠错 | [实施契约](docs/implementation-contracts.md) §9；[接入设计](docs/integration-discussion.md) Q32 |
| 查工程风险、保护措施和待验证实验 | [可行性报告](docs/feasibility-report.md) §8—9；具体实现取舍见 [OpenSpec design](openspec/changes/build-github-issue-agent-harness/design.md) 的 Risks / Trade-offs 与 Open Questions |
| 查实施范围、验收场景、阶段任务 | [proposal](openspec/changes/build-github-issue-agent-harness/proposal.md) → 按能力选择 [delta specs](openspec/changes/build-github-issue-agent-harness/specs/)；实现方式查 [design](openspec/changes/build-github-issue-agent-harness/design.md)，进度查 [tasks](openspec/changes/build-github-issue-agent-harness/tasks.md)，不默认遍历全部能力 |
| 查原始讨论、调研快照或历史复核 | [历史索引](docs/context/history/README.md)，先搜索所需主题 |

## 权威来源与更新位置

| 信息类别 | 权威位置和维护约定 |
| --- | --- |
| 长期协作规则 | [AGENTS](AGENTS.md)；长期约定变化才更新 |
| 文件职责与任务路由 | 本页；新增入口或职责变化时更新，不复制专题正文 |
| 项目用途和基本使用入口 | [README](README.md)；实际能力／使用方式变化时更新 |
| 当前目标、阻塞和接续点 | [NOW](docs/context/NOW.md)；状态实质变化时直接修订，不留多份当前快照 |
| 目录、配置、部署映射 | [MAP](docs/context/MAP.md)；位置或映射变化时更新，计划与实际分开 |
| 可复用操作步骤 | [RUNBOOK](docs/context/RUNBOOK.md)；有依据的方法变化时更新，结果证据另行引用 |
| 产品范围 | [需求基线](docs/requirements-baseline.md)；后续明确范围变化落在相关章节 |
| 已确认架构决定及理由 | [ADR](docs/adr/)；沿用现有记录结构，替代时显式关联旧决定，不另建 DECISIONS.md 复述 |
| 可编码的跨模块约束 | [实施契约](docs/implementation-contracts.md)；设计基线、流程和接入文档承载概览及解释，引用此处具体约束 |
| 术语 | [GLOSSARY](GLOSSARY.md)；其他文档使用同一含义 |
| 工程风险与验证缺口定义 | [可行性报告](docs/feasibility-report.md) §8—9；沿用为 RISKS 职责，不再复制风险台账。design 仅补充该变更特有取舍，当前验证状态写 NOW／对应任务证据 |
| 本变更验收场景与实施进度 | [delta specs](openspec/changes/build-github-issue-agent-harness/specs/) 承载场景，[tasks](openspec/changes/build-github-issue-agent-harness/tasks.md) 承载任务完成与证据引用；从上述基线和契约派生，不自行改写产品决定 |
| 历史证据 | 原历史文档及 [历史索引](docs/context/history/README.md)；只在需追溯或有价值归档时更新 |

同一事实不新增独立维护的正文副本。修改权威条目后，检查直接相关摘要或验收映射是否失效；出现未解决分歧时标明冲突并澄清，不静默选择。未发现具有独立用途的 PROJECT_NOTES.md，本次不新建。
