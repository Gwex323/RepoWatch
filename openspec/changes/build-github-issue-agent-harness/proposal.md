# Proposal

## Why

本地开发者需要在多个 GitHub 仓库中持续发现缺陷、处理 Issue 并跟进 PR，同时要求修改有真实验证证据、执行可暂停恢复、经验可跨任务复用。现有仓库已形成完整需求、架构与实施契约，但尚无产品代码或行为规格，需要把已确认决定转化为可实施、可验收的最终系统规划。

## What Changes

- 建设本地模块化单体：FastAPI API 与独立 Worker，React／TypeScript／Vite 管理界面，SQLite WAL 为默认持久化，预留 PostgreSQL 适配。
- 用 LangGraph＋Deep Agents 实现处理与独立只读审查两类角色；所有模型分诊、扫描、修复、返修及提炼统一串行，程序掌握状态、预算、证据和发布。
- 每次导入先完成固定代码版本的全仓深度扫描；覆盖／证据缺口在原预算内自动补全，无法恢复后全局暂停，目标分支变化后补扫。用户选择本地缺陷候选后，由系统先查相似 Issue，关联已有 Issue 或在未发现相似项时创建 Issue。
- 按仓库手动选择／标签准入／全部合格 Bug 模式处理已有／新增 Issue，自有或有维护权限仓库默认全部合格 Bug、第三方默认标签准入；六类分诊区分缺信息与执行阻塞，尊重他人占用及敏感披露。默认不 assign／评论，认领评论可显式启用；严重程度及老化参与排序，输入／标签／assignee 等变化重新分诊。功能和大规模重构等待方案与验收规范批准。
- Issue 与受管 PR 一一对应，双方稳定身份有唯一约束，返修更新原 PR，关闭后不另建；不将多个独立 Issue 合并为一个 PR。取得有效修改前后、回归与独审证据后才发布，外部竞争 PR 保留为观测来源。
- 支持有维护权限的公共／私有仓库及第三方公共 Fork 路径；建修复分支前和发布前检查 Issue 的开放 PR，同一问题则关联等待、确认无关则继续，仅关系不明由用户决定；已合入目标分支的相关 PR 直接核验问题是否解决；跟进 CI／人工评审并在原任务内返修，不自动合并。
- 所有人工介入节点在 UI 提供证据、具体处理入口及持久结果，覆盖候选／Issue／PR、方案、任务有效性、排障与恢复；人工决定后重核前置条件，不能以确认跳过证据门槛或自动解除暂停。
- 持久记录真实进展、重复失败及状态振荡，达到无进展阈值时提前停止；复杂任务有进展但预计额度不足时，在耗尽前通过 UI 申请用户批准的有限追加。追加保留累计消耗和检测历史，耗尽前的正常续期等待不触发全局故障，已阻塞不因加额自动恢复。
- 以活跃仓库约每 2 小时、空闲仓库每天一次的可配置频率巡检；巡检只同步数据，模型分诊由 Worker 执行。
- 支持多供应商模型 API、按角色选模型、能力准入与配置快照，以及分仓记忆、去敏共享经验、本地混合检索和人工管理。
- 按最终架构拆为 P0—P6：验证契约→内核→扫描→修复发布→持续跟进→完整记忆→本地产品化；保留 E1—E20 验收实验及其阶段门槛。

## Capabilities

### New Capabilities

- `local-control-plane`：本地浏览器界面、配置与人工控制、状态和证据查看，以及控制接口访问约束。
- `task-orchestration`：持久串行队列、执行权、预算累计、正常等待、全局暂停、诊断与人工恢复。
- `repository-workspaces`：多仓导入、贡献路径、受管快照、Docker 工作区及模式权限隔离。
- `deep-repository-scan`：完整清单、模块覆盖、基线、自动补全、版本变化补扫及本地缺陷候选。
- `github-synchronization`：已有／新增对象同步、可配置巡检、分页去重、持久水位、反馈信号和对象生命周期。
- `model-provider-management`：手工／JSON 导入、宿主密钥保管、Chat Completions／Responses、角色配置及能力准入。
- `agent-runtime`：LangGraph 阶段、Deep Agents 工具循环、两类核心角色和结构化串行交接。
- `repair-verification`：任务验收规范、有效复现、回归和基线比较、冻结候选、独审及版本化证据包。
- `pull-request-lifecycle`：受控发布、分支与远端对账、PR 专属 CI、人工评审返修及交付状态。
- `experience-memory`：有来源的成功／失败经验、范围与去敏、混合检索、纠错失效、删除及索引重建。
- `local-operations`：Windows 启停、数据库迁移、备份恢复、存储与产物保留、部署与语言适配验收。

### Modified Capabilities

无。当前 OpenSpec 主规格清单为空，本次全部为新增能力。

## Impact

提案建立时项目仅有文档；实施将新增 Python 后端／Worker、前端、受控执行与发布适配、数据库迁移及验证夹具。当前 P0 进展以 [tasks](tasks.md) 和 [P0 报告](../../../experiments/results/p0.md)为准。外部依赖涉及模型 API、GitHub API、Docker／WSL2 和本地 embedding 资产；模型与 GitHub 凭据留宿主。

本提案描述完整规划范围，规划产物不代表实现完成。部署范围为本地单用户，首批适配 Python 与 JavaScript／TypeScript Web／CLI 项目；多语言扩展保留接口，托管 MDA、并行修复、多用户 SaaS 和自动合并不在本次目标内。方案依据：[需求基线](../../../docs/requirements-baseline.md)、[设计基线](../../../docs/design-baseline.md)、[实施契约](../../../docs/implementation-contracts.md)与[可行性报告](../../../docs/feasibility-report.md)。
