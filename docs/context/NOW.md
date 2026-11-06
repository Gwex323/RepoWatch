# 当前状态与接续

核验日期：2026-11-06，范围为 P3 修复、候选 Issue、受控发布与本地验收。P2 已提交为 `625fb2c` 并推送至公共仓库 `Gwex323/repowatch`。当前位于 `feature/p3-repair-publication`，已接入分诊／批准、修复探针、冻结候选、回归／独审、外部合并核验、候选 Issue 查重和直接／Fork 发布适配。P3 15 项中 11 项已验收，整体任务为 52／89；7.1／7.2／7.3／7.7 等待真实受控 GitHub 证据，P3 整体尚未放行。代码未提交／推送；P4—P6 和最终用户验收未完成。

## 当前目标与进展

- P0 与全部 P1 任务已完成。P1 提供正式串行执行位、累计预算和有限续期、实际命令监督、显式恢复、角色能力／有限重试、回环会话／API／UI、持久无进展检测及 GitHub 只读来源接口。E1 费用仍待按账单核实。P0 的对象交接、恢复／调度及编码选型结论沿用[门槛汇总](../development/p0-summary.md)和[编码资产评测](../development/embedding-evaluation.md)，不替代后续真实仓库验收。
- 需求与最终架构已有基线、实施契约和 ADR，来源路由见 [项目索引](../../PROJECT_INDEX.md)。本次不重启需求访谈。
- 活动变更为 `build-github-issue-agent-harness`：[proposal](../../openspec/changes/build-github-issue-agent-harness/proposal.md)、[design](../../openspec/changes/build-github-issue-agent-harness/design.md)、[11 个能力的 delta specs](../../openspec/changes/build-github-issue-agent-harness/specs/) 和 [tasks](../../openspec/changes/build-github-issue-agent-harness/tasks.md) 已存在。按当前勾选计为 52／89；P3 尚在验收，本变更不归档。
- 开发安装见[开发入口](../development/README.md)，API／Worker 与人工入口见[本地控制](../development/local-control.md)，P1 引擎验收见[运行验收](../development/runtime.md#复跑与实验映射)。P2 扫描已通过阶段验收；P3 修复与发布适配已存在，当前 schema 为 `0021_distillation_state`，测试只迁移临时数据库。尚未执行真实 GitHub Issue／PR 写入、真实模型修复或完整产品用户验收。
- 已补充目标分支 SHA 读取和仓库手动“检查更新”的规划契约与验收场景；实现和运行验证仍待对应阶段完成。
- 最新决定：本地扫描缺陷先完整查重再创建或关联 GitHub Issue；外部开放 PR 同问题等待、无关继续、关系不明请求用户判断。P3 已接入这些入口及人工决定持久回读，远端 PR／评论变化会递增反馈并重新分诊。已合并 PR 须验证提交进入当前目标历史并运行真实探针，已解决记为 `externally_resolved`，目标仍失败则以当前版本继续验证；不会凭 `merged` 自证解决。
- 最新循环与预算决定：按真实产物检测无进展及振荡；复杂任务有进展但预计额度不足时，耗尽前正常等待用户批准有限追加，账本与检测历史不清零；已耗尽／无进展等仍全局阻塞且不因加额自动恢复。初值与恢复条件见[实施契约 §3](../implementation-contracts.md#无进展检测)。预算、检测、UI 和正式图集成已完成受控验收；真实扫描／修复事实生产及生产样本阈值校准由后续适配补齐。
- 最新准入与交付决定：仓库三种准入模式及默认值、六类分诊、他人占用／敏感排除、默认不 assign／评论而可启用认领评论、人工优先级与自动严重程度／老化、相关输入变化重分诊已明确；受管 Issue／PR 双向生命周期唯一、返修原 PR、关闭不新建，外部 PR 作为观测保存。权威规则见[准入契约](../implementation-contracts.md#issue-准入与分诊)及[一一对应契约](../implementation-contracts.md#issue-与-pr-一一对应)，仍待产品实施验证。

## 阻塞与待核实

- 用户已授权继续 P3 开发；当前分支 `feature/p3-repair-publication`，保留原有未跟踪临时目录，未提交／推送。2026-11-06 沙箱外只读核实 `gh api user` 为 `Gwex323`；沙箱内认证报告失效是环境可见性差异。真实 GitHub 写入验收还需明确指定可写测试上游与 Gwex323 Fork 配对；不能把项目代码仓库或其他账户当作默认测试授权对象。
- Docker Desktop 此前停止，首次重跑为 `466 passed, 4 failed, 1 skipped`；四项均因引擎不可用。启动已安装 Docker 后容器专项 `15 passed`，后续全套 `491 passed, 1 skipped`。沙箱内 TestClient 挂起在沙箱外基础测试 `2 passed` 后解除；这些环境失败不再作为代码阻塞。最终证据防伪／原 PR 更新等后续修改的最新报告见下方，不沿用旧全量宣称最终冻结通过。
- P0 六项关键切片均已通过，P1 已放行；但费用账单、真实 GitHub 写入、完整 E11 安装／启停／交付链路、LFS 实体与子模块对象仍未完成，含这些内容的仓库保持阻塞。1.9 主选 `jinaai/jina-embeddings-v2-base-zh`（Apache-2.0、768 维、0.64GB、离线加载通过），代码备选为 `jinaai/jina-embeddings-v2-base-code`；评测仅覆盖 22 条语料／12 条查询，生产召回与运行时锁仍需在 P5 前复核。编码运行时未加入默认依赖，模型资产未固定摘要。
- P1 已无需要人工介入的开发阻塞。普通仓库执行需 P2 的身份／工作区／Runner／扫描接入；本轮只读 GitHub 通过 HTTP 夹具验证，未访问真实仓库。P1 通过不代表真实修复发布、完整产品部署或最终用户验收通过。
- 此前 3.3 的凭据预检阻塞已解除：用户完成真实复测，新报告配置摘要匹配，8／8 通过，23 次请求全部 HTTP 200；费用仍待核实。其后 P1 集成已完成，未重复执行付费复测，证据见[P1 供应商](../development/providers.md#当前验证与真实-e1-复测)。

## 下一步

1. P3 本地开发及验收已继续推进：共享 SDK＋真实 Docker、三类验收、独审补查／返修、无进展及续期、认领评论故障和只读回执均有记录。7.1／7.2／7.3／7.7 的真实远端分支竞争、直接／Fork 写入与回执仍缺受控仓库证据，保留未勾选，不进入 P4。
2. 提供获授权的可写测试上游与用户 Fork 配对后，执行 7.3／7.7 真实链路并记录上游／发布身份、base／head 与实际 SHA；目前不写入真实仓库、不跨账户自行创建 Fork。继续沿用原预算、执行权和唯一 PR 生命周期约束。
3. 完整安装启停、备份、产品级故障集及真实修复发布仍按 P6 和对应阶段验收，不提前标记最终产品完成。

## 证据入口与时效

- 2026-11-06 P3 本地重试：末次完整 Docker-enabled 后端 `507 passed, 1 skipped in 800.44s`，JUnit `.cache/p3-release-review-final.xml`（唯一跳过仍为独立开关控制的 Git 对象交接实验）。此前 `491 passed, 1 skipped`／`500 passed, 1 skipped` 被该报告取代；全量启动后新增创建前查重重读、专用回执模式及测试，最后冻结定向回归 `.cache/p3-final-delta.xml` 为 `68 passed`，覆盖发布／候选／Worker／调度／API／SDK 分诊；创建竞争 `6 passed`、`.cache/p3-worker-receipt-final.xml` 的 `10 passed`、`.cache/p3-scope-final.xml` 的 `7 passed`。真实 SDK／Deep Agents＋Docker 串行修复 `1 passed`（`.cache/p3-sdk-docker.xml`），证据防伪与原始日志绑定真实 Docker `6 passed`（`.cache/p3-proof-binding.xml`），独审补查／返修 `2 passed`（`.cache/p3-review-reentry.xml`），真实进展后有限续期 `1 passed`（`.cache/p3-repair-renewal.xml`），认领评论默认不发／成功／丢回执 `3 passed`（`.cache/p3-claim-contracts.xml`）。Edge 十项报告 `.cache/p3-browser/1793942951731/report.json`：保存／批准／刷新／输入失效、暂停操作、无隐式执行、候选查重决定、PR 关系决定、专用只读回执排队及任务归属 Runner 日志读取。Ruff、前端 TypeScript／Vite、OpenSpec strict、四份直接关联文档的 136 处本地链接目标及差异检查通过（未全面复核历史锚点／外链）。模型 HTTP 与远端发布使用受控夹具；真实模型、真实 GitHub 写入、用户数据库迁移、提交／推送均未执行。

- 2026-11-06 P3 首批：代码冻结后的完整 Docker-enabled 后端回归 `445 passed, 1 skipped in 453.44s`，JUnit `.cache/p3-frozen-full-20261106.xml`；唯一跳过仍为独立开关控制的 Git 对象交接实验。迁移／任务记录／准入及验收专项 `54 passed`，报告 `.cache/p3-final-unit.xml`；新增实际 SDK／Deep Agents 分诊测试使用模拟 HTTP，真实模型未调用。Edge 六项验收通过，报告 `.cache/p3-browser/1793929441231/report.json`，覆盖规范保存、批准、刷新持久化、输入变化失效、暂停操作和无隐式执行。Ruff／格式、前端 TypeScript／Vite、OpenSpec strict、128 处文档本地链接及差异检查通过。冻结后的全量报告取代早期运行期间修改迁移所产生的混合版本失败结果；6.1 仍未全部完成，不声明 P3 门槛通过。本轮未操作真实 GitHub、未调用付费模型、未迁移用户数据库、未提交或推送 P3。

- 2026-11-05 P2 复核修复：最终启用 `REPOWATCH_DOCKER_TESTS=1` 的全量后端 `384 passed, 1 skipped`，本地 JUnit 报告 `.cache/p2-review-final-v3.xml`。此前全量发现的无仓库诊断发布守卫回归已修复并包含在最终套件。真实 Docker 覆盖固定镜像、临时分区、Python wheel／Node 本地依赖离线安装、声明摘要不变、控制停止、结果复用、重建与删除；正式扫描协调器另经真实 Deep Agents 工具／SDK 模拟 HTTP 完成三次调用、原预算记账、宿主覆盖事实、工作区退役与就绪交接。P2 Edge 六项报告 `.cache/p2-browser/1793853681290/report.json`，P1 Edge 十二项复跑截图 `.cache/p1-browser/1793854566359/control.png`。Ruff、TypeScript／Vite、OpenSpec strict 及四份文档 121 处链接检查通过；未调用真实付费模型或 GitHub 写入，完整修复发布未执行。
- 2026-11-05 中间复核（已被下方最终全量结果取代）：当时末次 Docker／非 Docker 全量运行挂起并中止，相关回归为 `70 passed` 与 `24 passed, 3 skipped`；随后新增供应商失败组合与 Worker 恢复交接测试。此中间记录不是最终门槛结论。
- 2026-11-05 按序验收：4.5 根锁离线依赖与缺环境报告、5.2 基线状态持久核验、5.4 补扫／拆分／证据重收、5.6 版本批次、5.7 分支与依赖门禁、5.8 UI 操作及 5.5 Worker 跨 Run 恢复交接有直接回归；扫描／正式运行／调度合并 `76 passed`。真实 Docker Python wheel／Node 本地依赖和工作区回归 `12 passed`；Edge 报告 `.cache/p2-browser/1793864390192/report.json` 验证候选选择／排除、任务导航、补充材料、暂停与取消。最后补丁后的全后端套件仍未在本机稳定通过，P2 最终验收待该门槛。
- 2026-11-05 最终全量复核：用户报告 `402 passed, 1 skipped in 350.52s`；该结果被记录为用户提供的验收证据。本地复跑报告 `.cache/p2-final-20261105.xml` 为 `399 passed, 3 failed, 1 skipped`，3 项都是 Docker Desktop `npipe` 权限拒绝导致的 Docker 操作失败（Python/Node 离线安装、真实 Docker 工作区创建）；不是产品测试断言失败，但也不能计为通过。当前 P2 代码／专项验收齐全，最终全量门槛需在允许 Docker named pipe 的授权环境重新取得绿色报告。报告由本机尝试生成；未提交／推送／部署。
- 2026-11-05 Docker 恢复后最终报告：`402 passed, 1 skipped in 460.62s`，本地 JUnit `.cache/p2-final-docker-ready-20261105.xml`，403 tests、0 failures、0 errors。Docker 启动后先前三项阻塞测试单独复跑 `3 passed`。唯一跳过为 `test_docker_object_handoff_end_to_end`，受独立 `REPOWATCH_DOCKER_E2E=1` 开关控制，验收 LFS／子模块交接，不属于扫描 P2；此报告取代上一条本机 Docker 权限受阻结果。

- 2026-11-04 P1 最终集成：`experiments/run.py p1-kernel` 全量 316 项通过、1 项 Docker 跳过，报告 `experiments/results/local/p1-kernel.json`。覆盖 E7 多进程／父子执行权、E14 累计预算与持久检测、E18 恢复和 E20 最小存储故障；9 项正式 SDK／图验收和 6 项 GitHub 只读夹具均通过。Ruff 检查／格式、80 项依赖离线锁、前端 TypeScript／Vite、wheel 及解包后隔离导入／`0014_read_sources` 重复迁移／暂停 Worker、OpenSpec strict 均通过。实际 Edge 12 项操作通过，截图 `.cache/p1-browser/1793780726451/control.png`；使用临时回环宿主和测试数据，未调用外部模型／GitHub。真实 E1 用户复测另为完整矩阵 8／8、23 次 HTTP 200，见[P1 供应商证据](../development/providers.md#当前验证与真实-e1-复测)。费用账单、完整产品链路与最终用户验收未完成；未提交／推送或部署。

- 2026-11-04 P1 2.4：E14 预算切片 68 项通过，最终全量后端回归 213 项通过、1 项 Docker 跳过；Ruff、80 项依赖的离线锁校验、OpenSpec strict、wheel 构建及隔离包的迁移／授权／续期刷新／旧用量耗尽验证通过；7 份直接关联文档的 127 处本地链接／锚点检查通过。实验入口、真实 SDK 模拟 HTTP、旧库升级与阶段边界见[累计预算](../development/budgets.md)，本地报告 `experiments/results/local/e14-budget.json`。未执行真实供应商／GitHub、完整 E14 检测、前端或用户验收，未提交／推送。
- 2026-11-04 P1 2.1—2.3：后端回归 145 项通过、1 项 Docker 用例按既有开关跳过，补强后的正式调度验收 14 项通过；Ruff、80 项依赖的离线锁校验、OpenSpec strict 校验、wheel 构建及隔离包迁移通过。6 份直接关联文档的 116 处本地链接／锚点检查通过。范围及复跑入口见[业务持久化](../development/persistence.md)；未执行真实供应商／GitHub 写入、前端构建、产品启停或用户验收，未提交／推送。
- 2026-11-04 P0 实施证据：E1 8／8、对象交接 15 项、恢复 12 项、调度 15 项检查通过；恢复切片回执丢失后仅 1 次创建调用、1 个 PR，调度切片暂停期间模型调用 0 次。编码资产报告 `experiments/results/local/embedding-20261104T005203865138Z.json` 显示 FTS5／`vec0` 加载、三种候选离线加载与选型结果；主选中文模型中文 recall@3／MRR 1.000／0.889、代码 0.833／0.848。P0 汇总见[门槛汇总](../development/p0-summary.md)：依赖锁无新增生产依赖，协议 v1 不变，工作量重估 19—27 人周。详见 [P0 报告](../../experiments/results/p0.md)。依赖空环境复装和前端构建属于首批 1.1 的验证，本轮未重跑未变动前端。历史文档结果不替代本轮实现证据。
- 2026-11-03 本次 Issue 准入／一一对应修订验证：活动变更 OpenSpec strict 校验通过、0 问题；14 份涉及文档的 74 处本地链接／锚点、代码围栏、受影响 6 份规格的 154 个场景结构、指定契约边界及流程绕过路径文本检查通过。任务仍为 89 项未完成、0 项完成；未执行产品测试、模型实验、GitHub 写入或运行验收，老化间隔等初值待实施校准。
- 2026-11-03 本次无进展／预算续期修订验证：活动变更 OpenSpec strict 校验通过、0 问题；13 份涉及文档的 78 处本地链接／锚点、代码围栏、受影响 3 份规格的 77 个场景结构及契约关键边界检查通过。任务仍为 89 项未完成、0 项完成；未执行产品测试、模型实验或运行验收，阈值仍需后续 E14 样本验证。
- 2026-11-03 本次人工 UI／PR 契约修订验证：活动变更 OpenSpec strict 校验通过、0 问题；10 份涉及文档的 56 处本地链接／锚点、代码围栏及指定旧 PR 规则残留检查通过。任务仍为 89 项未完成、0 项完成；未执行产品测试、模型实验或运行验收。
- [当前复核结果](../document-review.md#本轮验证结果)：本轮已执行 OpenSpec strict 校验；规划完整不表示实现或运行验收通过。
- [基础文档建立时的检查](RUNBOOK.md#本轮核验范围)与[首次文档复核](../document-review.md#检查范围与剩余验证)：7 个文件／101 处链接、33 份 Markdown 等数字分别属于各自历史范围。
- 需要重新核验当前规划时按 [RUNBOOK](RUNBOOK.md#检查现有-openspec-规划) 执行；不要沿用旧日期或仅凭产物存在宣布实现完成。
- GitHub 研究来源见 [历史索引](history/README.md)，固定日期与提交的源码事实不等于当前依赖安装验证。
