# 开发入口

日期：2026-11-04。当前 P0 与 P1 开发门槛已通过，包含正式持久化／调度／预算、模型、控制台及共享运行引擎；当前证据见 [NOW](../context/NOW.md)，完整产品安装及后续阶段仍按 [tasks](../../openspec/changes/build-github-issue-agent-harness/tasks.md) 推进。P0 原型结果见 [P0 报告](../../experiments/results/p0.md)与 [P0 汇总](p0-summary.md)。

P1 的迁移、跨库窗口及只读事实源见[业务持久化](persistence.md)，计量和有限续期见[累计预算](budgets.md)，暂停／诊断／异常对账见[控制与恢复](control-recovery.md)，宿主凭据／角色／有限重试见[P1 供应商](providers.md)，开发启动与 UI 见[本地控制](local-control.md)，正式图及 E7／E14／E18／E20 内核映射见[运行引擎](runtime.md)；下文 P0 原型说明保留其原验证边界。

## 环境与安装

前置条件：Python 3.12、uv、Node ≥22.12、pnpm 9.12.0；本轮验证版本见报告。以下从项目根目录运行，依赖全部来自锁文件，不修改全局工具：

```powershell
uv sync --project backend --locked --python 3.12
pnpm --dir frontend install --frozen-lockfile --registry=https://registry.npmjs.org
```

`backend/.venv` 和 `frontend/node_modules` 是开发安装目录。uv／pnpm 可通过自己的配置指定缓存；本次使用的缓存和第二个干净环境位于已忽略的 `.cache/`，无设备私有路径依赖。未下载编码模型资产或 Playwright 浏览器。

## 验证

```powershell
backend/.venv/Scripts/python.exe experiments/run.py p0-local
backend/.venv/Scripts/python.exe experiments/run.py git-handoff-contracts
backend/.venv/Scripts/python.exe experiments/run.py git-handoff
backend/.venv/Scripts/python.exe experiments/run.py e8-contracts
backend/.venv/Scripts/python.exe experiments/run.py e8
backend/.venv/Scripts/python.exe experiments/run.py e13-contracts
backend/.venv/Scripts/python.exe experiments/run.py e13
backend/.venv/Scripts/python.exe experiments/run.py embedding-contracts
backend/.venv/Scripts/python.exe experiments/run.py embedding
backend/.venv/Scripts/python.exe -X utf8 experiments/run.py e14-budget
backend/.venv/Scripts/python.exe -X utf8 experiments/run.py p1-kernel
uv lock --check --project backend --offline
backend/.venv/Scripts/ruff.exe check backend/src backend/tests backend/migrations experiments/run.py
backend/.venv/Scripts/ruff.exe format --check backend/src backend/tests backend/migrations experiments/run.py
pnpm --dir frontend build
node experiments/p1-browser.mjs
```

实验入口还支持 `bootstrap`、`protocols`、`e7`、`runtime`、`e1-contracts`、`git-handoff-contracts`、`e8-contracts`、`e13-contracts`、`embedding-contracts` 单独切片，本地日志写入已忽略的 `experiments/results/local/`。真实 E1 的配置、消耗上限及独立报告见[供应商验证](e1-providers.md)，Windows→Linux 对象交接见[Git 对象交接](git-handoff.md)，回执与旧代次恢复见[恢复模拟](recovery-simulation.md)，调度依赖见[调度模拟](scheduling-simulation.md)，本地编码资产与检索降级见[编码资产评测](embedding-evaluation.md)。`embedding` 评测依赖 `.cache/embedding-eval` 与模型缓存，未写入项目锁；报告中的通过范围仅限执行的切片。Windows 沙箱若限制子进程／临时目录，需要批准相应测试／构建；不能据沙箱失败推断产品故障或记录为通过。

正式 API 通过 `repowatch.api.host:create_host_app` 组装；无服务参数的 `create_app()` 仅保留工程健康夹具。前端已提供 P1 控制台，API／Worker 开发启动、数据目录与一次引导会话见[本地控制](local-control.md#开发启动与边界)。`p1-kernel` 为全量后端回归，浏览器脚本使用本地 Edge／临时数据；普通 Worker 仍需 P2 的真实工作区适配，不将默认缺失适配当作仓库执行已具备。

## 数据与执行原型（1.3）

首版迁移位于 `backend/migrations/versions/0001_core_draft.py`，由 Alembic 和 SQLAlchemy 连接执行。它是 SQLite 新库草案，测试在临时文件中迁移；默认库路径和配置不能作为正式产品部署方案，降级入口明确拒绝执行。

草案覆盖仓库、来源唯一主任务、预算归属、运行、全局控制与租约、六类分诊、信号、产物、发布意图、受管 PR／外部观测、进度、扫描依赖、事件及人工操作。`tasks` 的仓库／Issue、来源键唯一；受管 PR 双向唯一且通过复合外键绑定原任务，关闭记录仍占用绑定。其余最终字段及业务端口在 P1 按实施契约继续落实。

`adapters/storage/prototype.py` 是 P0 原型文件，使用 sqlite3 独立连接验证短事务，连接启用外键和 5000ms 有限锁等待，迁移时开启 WAL。领取以 `BEGIN IMMEDIATE` 原子写入运行与唯一租约；心跳另开短事务，模型／仓库命令不在事务中执行。初始全局暂停，仅实验夹具显式解除。P1 的正式业务端口、迁移、控制和运行接入见[本地控制](local-control.md)与[运行验收](runtime.md)。

租约过期不自动接管，旧代次被拒绝；释放必须明确确认旧命令已停止。这里的确认值是 P0 原型测试输入；P1 已在正式控制与运行路径接入命令生命周期核验、队列领取、预算账本和有界安全停止，P2 再接入真实工作区 Runner。

## 本地 Agent 原型（1.4）

`runtime/prototype.py` 实际调用 Deep Agents 及其 LangGraph 图，用脚本模型驱动真实工具循环与 ToolStrategy 结构化交接。受管子任务经 `CompiledSubAgent` 同步执行；信封由 Harness 预先绑定，模型不能更换预算或提升子任务工具权限。子任务可绑定独立的显式配置快照。

所有模型及工具活动共享步骤上限和串行互斥；图以 `max_concurrency=1` 调用，父模型在子任务期间等待。默认框架工具在模型入口过滤，工具执行入口再次拒绝未授权调用；框架默认 general-purpose 子任务也被拒绝，只有预先绑定的子任务可调用。独审重新建图／会话，不继承处理消息，仅获得其信封及授权工具。

原型后端是内存 StateBackend，不连接宿主文件系统、Docker 或托管 MDA。测试使用夹具工具，未证明真实容器隔离；这仍是 P0 原型的边界。P1 已建立持久业务执行权、正式预算计量、持久检查点和无进展历史，正式运行入口见[运行验收](runtime.md)。本节测试关闭外部 tracing，不调用真实供应商，不能替代 E1。

## Git 对象交接原型（1.6）

`runtime/git_handoff.py` 在 Windows 侧用 Git 底层对象建立夹具和 bundle，在 Linux 容器命名卷中克隆、检出、修改并导出候选 bundle，再把候选对象导入宿主独立裸仓库做树、内容和对象完整性对账。夹具覆盖 LF／CRLF、大小写并存、可执行位、符号链接、LFS 指针和子模块 gitlink；LFS 实体与子模块对象本轮缺失，交付明确阻塞。

容器使用 `--network none`、`cap-drop=ALL`、`no-new-privileges`，不挂载宿主目录或传递凭据。该切片只证明对象与工作树内容可在 Windows／Linux 间一致往返，不覆盖完整 E11 启动与会话链路，也不覆盖完整 E15 扫描或发布器。详见[Git 对象交接](git-handoff.md)。

## 模拟远端恢复原型（1.7）

`runtime/remote_recovery.py` 用 SQLite 保存发布意图、暂停状态和执行代次，用进程内模拟远端验证“创建成功但回执丢失”。恢复协议要求先暂停并按稳定操作键对账，不重发；旧命令未确认停止时拒绝恢复；恢复后递增代次，旧代次迟到结果不能覆盖新状态。另覆盖 PR 正常等待重启不误报崩溃，以及排障不解除暂停。

该切片不连接真实 GitHub，不实现完整租约、多 Worker 竞争、崩溃注入或人工 UI；详见[恢复模拟](recovery-simulation.md)。

## 调度依赖原型（1.8）

`runtime/scheduling_prototype.py` 使用 Alembic 草案数据库和现有执行位原语，验证暂停时同步只写信号、模型与领取被拒绝；等待扫描的修复任务不占执行位；扫描完成在同一事务内唤醒依赖方；普通取消只解除自身依赖；撤销扫描预算时停止扫描并保留其他依赖；PR 正常等待不误报崩溃。

该切片是单进程原型，不覆盖多 Worker 竞争、真实 API 生命周期、完整状态机或前端入口；详见[调度模拟](scheduling-simulation.md)。

## 下一门槛

P1 开发门槛已通过，下一阶段为 P2 仓库／工作区与扫描；接续点以 [NOW](../context/NOW.md) 和 [tasks](../../openspec/changes/build-github-issue-agent-harness/tasks.md) 为准。费用账单、LFS 实体、子模块可得性、真实 GitHub 写入和小样本召回的生产泛化仍待核实；P0 内存／草案原型不作为生产状态机。
