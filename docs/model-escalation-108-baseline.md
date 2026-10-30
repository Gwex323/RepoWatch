# 模型升级编排实施基线（Issue #108）

采集时间：2026-10-31 00:59（Asia/Shanghai）

本记录只包含脱敏配置、任务身份和公开远程状态，不包含供应商密钥、密文、提示词、模型原始输出或完整私有服务地址。

## 代码与数据库

| 项目 | 基线 |
| --- | --- |
| 工作分支 | `feature/model-escalation-108` |
| 基础提交 | `5c25840`（`origin/codex/initial-upload`） |
| 数据库 | `data/repowatch.db`，11,886,592 字节 |
| SQLite 完整性 | `PRAGMA quick_check` = `ok` |
| 模型工作流 | 已启用，配置版本 41 |
| 系统故障记录 | 已启用，版本 1 |
| 自动修复 | 已启用，版本 6 |
| 系统协调 | 已启用，版本 2，未授权备用模型 |

## 供应商与角色

供应商目录包含以下已保存模型；密钥只记录为存在：

| 供应商 | 模型 | 版本 | 密钥 |
| --- | --- | --- | --- |
| `deepseek` | `deepseek-flash`（medium）、`deepseek-v4-pro`（high） | 1 | 存在 |
| `智谱` | `glm-5.3`（high）、`glm-5.3-flashx`（medium）、`glm-5.3-flash`（medium） | 2 | 存在 |
| `本地` | `gpt-6.1-sol`（high）、`gpt-6-astra`（high） | 2 | 存在 |
| `火山coding` | `glm-5.3`（high）、`glm-5.3-flash`（medium）、`kimi-k3`（auto）、`deepseek-v4-pro-ga-260813`（auto） | 3 | 存在 |

六个角色的当前路由均为 `glm-5.3`：

- `system_coordinator`、`repository_analyst`、`problem_investigator`、`reproduction_assistant`、`independent_reviewer` 使用火山coding。
- `repair_agent` 使用智谱。
- `system_coordinator` 的独立配置为空，依赖显式角色选择。

因此基线状态没有启用本次要求的 `deepseek-flash` 监控与 `deepseek-v4-pro` 修复分流。

## 目标仓库

| 项目 | 基线 |
| --- | --- |
| 仓库 | `Gwex323/gateway-model-assistant` |
| 本地登记 ID | `repo_e5540642924d482d` |
| GitHub 默认分支 | `main` |
| 默认分支提交 | `9870a74369a81c53f9f4edb384598b240a16cbc0` |
| 已登记环境 | `env_fcf4645bfb3245b8` |
| 仓库权限 | 管理员、维护、写入、分流、读取 |
| 已验证工作台账号 | `gwx541`，数据库状态 `verified`，作用域 `repo` |

## 历史任务与故障

- 最近一次分析 `analysis_8b72bb9f2f08492c` 失败，原因为 `analysis_storage_failed`，源码 revision 与当前默认分支提交相同。
- 同 revision 的近期分析还出现 `analysis_scope_read_not_followed`、`analysis_provider_timeout_exhausted`、`analysis_output_truncated` 和 `analysis_evidence_unverified`。
- 历史修复 `repair_0e2c6446a06d4626` 已创建 Issue #3 和 PR #4，分支为 `repowatch/repair-0e2c6446a06d4626`，提交为 `f46febc832af6d8238fa5a37e30027a20861cb3a`。
- GitHub 当前显示 Issue #3 和 PR #4 均为 open，PR #4 未合并。
- 当前没有 queued、running、waiting_model 或 recovering 状态的分析与修复任务。
- 目标仓库共有 8 条活动故障聚合记录：2 条证据校验恢复中、2 条供应商超时恢复中、1 条输出截断恢复中，以及 3 条受阻记录。

## 实施前约束

- 原生产运行工作台使用同一数据库，不能在未备份和未核验活动任务时直接切换。
- 新策略默认关闭，启用后不得改写现有角色选择或供应商凭据。
- 真实运行必须从当前默认分支的同一 revision 开始，不能把历史 PR #4 当作本次验收结果。
- 没有真实有效问题或审查未通过时不得创建目标仓库 PR。
- 所有远程写入继续要求已验证账号、幂等回执和明确权限；模型不能直接获得发布工具。
