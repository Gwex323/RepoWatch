# 目录、配置与运行对象映射

核验日期：2026-11-04。本轮范围：P1 正式存储、调度／预算／恢复、供应商、图引擎、控制台与只读来源的位置；原 P0 工具与 Docker 检测结果仍见 [P0 报告](../../experiments/results/p0.md)，本轮未检测外部部署。

## 已存在的位置

所有路径均相对项目根目录；从 [README](../../README.md) 和 [AGENTS](../../AGENTS.md) 所在目录定位根，不依赖个人设备绝对路径。

| 位置 | 对象与关系 |
| --- | --- |
| 根目录 README、AGENTS、PROJECT_INDEX、GLOSSARY | 用途、规则、路由、术语；职责见 [项目索引](../../PROJECT_INDEX.md) |
| `docs/` | 需求、架构、实施契约、流程与可行性报告 |
| `docs/adr/` | 已接受的架构决定；保留原位置 |
| `docs/research/` | 固定提交的调查证据，不是安装目录或依赖锁 |
| `docs/context/` | 当前状态、位置映射、操作手册及历史导航 |
| `docs/development/` | 开发安装、P0 原型边界、协议字段及 P1 正式持久化／预算／控制恢复／供应商说明 |
| `backend/pyproject.toml`、`backend/uv.lock` | Python 3.12 开发工程与锁定依赖 |
| `backend/src/repowatch/` | domain 协议／任务／预算／供应商对象，application 提供业务端口与持久进展／只读来源；adapters 提供 SQLite、Windows 作业／凭据、模型与 GitHub GET；runtime 提供正式图与原型，api／worker 提供正式开发入口及诊断派发，真实仓库计划待 P2 |
| `backend/migrations/`、`backend/tests/` | 保留 P0 迁移并接续正式 schema；P0 回归及 P1 存储／任务记录／调度／预算与旧库升级验收 |
| `frontend/` | React／TypeScript／Vite、`pnpm-lock.yaml` 及 P1 控制台；生产构建 `dist/` 由 API 同源提供，浏览器不承载任务执行 |
| `experiments/run.py`、`experiments/results/` | 本地切片及 E1 实验入口、公开脱敏报告；私有原始结果位于已忽略的 `results/local/` |
| `experiments/p1-browser.mjs`、`experiments/p1_browser_host.py` | P1 Edge 浏览器操作与临时回环宿主夹具，证据位于已忽略的 `.cache/p1-browser/` |
| `experiments/providers.example.json` | E1 配置模板，使用显式环境变量引用；实际配置 `providers.local.json` 已忽略，说明见 [E1](../development/e1-providers.md) |
| `openspec/config.yaml` | 当前规划 schema 为 `spec-driven`，内容使用中文、保留结构标题和 SHALL／MUST；配置中未声明外部 store |
| `openspec/changes/build-github-issue-agent-harness/` | 活动变更；元数据创建日期为 2026-11-03；proposal、design、tasks 及 specs 能力目录 |
| `openspec/specs/` | 当前仅占位；不能把活动 delta 当作已实现主规格 |
| `openspec/changes/archive/` | 当前仅占位；未来归档由对应工作流管理，本轮不移动规划 |

## 计划位置与版本边界

- 工程已按 P0 建立，最终模块细分见 [design：模块边界](../../openspec/changes/build-github-issue-agent-harness/design.md#1-modular-monolith-and-module-boundaries)，未实现模块仍属后续阶段。
- 技术路线见同一设计章节；本次锁文件和与调研版本差异见 P0 报告。`0.1.0.dev0` 是工程开发版本，未发布产品。
- API／Worker、业务数据库／图检查点和 Docker 工作区的目标关系见 [实施契约](../implementation-contracts.md) §2、§6、§10。P1 正式数据位置见[业务持久化](../development/persistence.md)，凭据与脱敏见[P1 供应商](../development/providers.md)，开发启动见[本地控制](../development/local-control.md)，正式图与后续适配边界见[运行引擎](../development/runtime.md)。安装部署及容器链路仍待后续项。

映射变化时只修订本页对应位置；具体操作写 [RUNBOOK](RUNBOOK.md)，当前推进状态写 [NOW](NOW.md)。
