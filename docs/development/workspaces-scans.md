# 工作区与扫描操作说明

本页记录 P2 已接入的操作与未验收边界，不代表阶段验收通过。当前进度以 [NOW](../context/NOW.md) 与 [任务清单](../../openspec/changes/build-github-issue-agent-harness/tasks.md) 为准。

## 导入与版本

先在仓库页核对上游、目标分支、直接贡献或自己的 Fork，再预检、保存。每次新导入增加导入代次、撤销旧就绪、建立完整扫描；重复相同人工操作不增加代次。扫描策略中的分块大小和带依据的排除项由 `scan_policy` 保存，实质变化增加策略修订。未经确认不能把已有 PR 改投其他目标。

扫描 Worker 在 fetch 前及完成前无条件读取 branch `commit.sha`，fetch 返回提交也必须匹配固定快照。发现漂移时登记新扫描、沿用原预算、重连等待依赖，不以旧 SHA 完成当前就绪。读取失败会撤销旧就绪并记录 `target_read_failed`；配置竞争拒绝应用旧结果。当前无定时巡检或仓库“检查更新”产品入口；该入口及租约合并在 P4 交付，不能把 P2 的内部核验端口当作已完成的 P4 同步。

## 隔离环境与依赖

宿主只持有凭据和裸 Git 对象，容器只挂载当前任务源码卷，不挂载业务数据库、Docker socket 或宿主凭据。源码卷只读；检查先复制到 `/work/source`，依赖目标与缓存位于 `/deps`，输出使用 `/out`。工作镜像及加载辅助容器使用已解析的镜像 ID。归档超过 8 MiB 时落入临时文件；单个 Git blob 与清单仍会占用内存，超大文件流式适配尚待验收。

依赖准备在网络隔离容器中按受检声明执行：根 `uv.lock` 使用 `uv sync --frozen --offline --all-groups`；根 `requirements.txt`／`requirements-dev.txt` 从镜像内 `/opt/repowatch/wheels` 安装至 `/deps/python`；根 npm／pnpm 项目按 `package-lock.json`／`pnpm-lock.yaml` 离线安装并禁止安装脚本。安装前后核验所有依赖声明摘要，源码卷只读，目标与缓存分置 `/deps`，业务锁文件不被修改。不会把 GitHub 令牌注入通用容器。缺工具、离线资源、受支持锁文件或私有凭据时记为 `unable`，并在基线证据／页面提供文件路径、失败步骤与原因；私有依赖应由受控宿主准备为镜像资产，当前不自动下载私有包。根 pyproject 未锁定、嵌套依赖项目和 JS workspace 不静默忽略，而是报告不支持缺口；明确由根 npm 锁引用的本地 `file:` 包可随锁安装。其他未支持声明／多包结构不能视为已适配。

不声明适用检查时，宿主以固定 Git 树识别依据记录 `not_applicable`；声明的检查不能因缺环境转为不适用。真实断言失败可记为 `existing_failed`，控制停止、超时、缺包、截断日志均不能记为代码既有失败。

## 查看与恢复

仓库页展开扫描，显式读取清单、分块、排除依据、日志基线与候选。覆盖数字只计算当前可读取且摘要一致的输入／结果产物。候选选择只保存本地 Issue 查重请求；Issue／PR 身份门禁之前不创建修复分支。

使用“打开扫描任务”进入任务详情，补充环境材料、修改 JSON 环境配置或取消。在待人工事项页核对阻塞原因与真实日志，填写处理说明、有限重试计划和成功条件，然后明确恢复；加预算不自动清除阻塞。重建前确认旧命令已停止；新环境预检失败保留旧工作区，删除过程先撤销数据库就绪、删除卷后登记 `removed`。

## 复跑与限制

使用 `backend` 已安装的 Python 3.12 虚拟环境运行相关 pytest；指定全新的 `--basetemp` 和 `-p no:cacheprovider`，避免读取无权访问的旧临时目录。真实 Docker 用例仅在显式 `REPOWATCH_DOCKER_TESTS=1` 下执行，不应把跳过视为通过。前端需分别执行 TypeScript、Vite 构建与浏览器操作验收。

2026-11-05 最终回归为 `384 passed, 1 skipped`，报告 `.cache/p2-review-final-v3.xml`。含真实 Docker 与 Python wheel／Node 本地依赖离线安装；旧对象交接实验仍因独立开关跳过。前端生产构建通过，P2 Edge 六项报告 `.cache/p2-browser/1793853681290/report.json`；P1 的有界重试、显式恢复等十二项 Edge 操作复跑通过。扫描专项覆盖 fetch／扫描结束漂移、失败读取持久阻塞、导入依赖替换与实际唤醒、预算沿用、失败重建保留旧环境、删除退役、尝试／结果幂等、动态拆分、截断补扫、失效输入／结果重收、旧环境批次拒绝完成及有效覆盖显示。最终全量后，仅增强正式扫描协调器测试并通过；该测试通过真实 Deep Agents 引擎和 SDK 模拟 HTTP 执行三次调用，验证宿主覆盖事实、原预算、工作区退役和完成交接，无真实付费请求。

全量之后，补充不适用证据的重收与批次建立失败时清理工作区，关联套件 `69 passed`，报告 `.cache/p2-review-last-focused.xml`。正式扫描协调器的成功／无进展／预算耗尽三种真实引擎测试通过：成功三次调用形成覆盖事实，连续三次无覆盖触发全局暂停，模型调用限额为一时只发出一次请求。随后补 Runner 检查日志重收，实际 Docker 与 Python／Node 离线依赖共 `12 passed`，报告 `.cache/p2-review-docker-last.xml`；重建原预算计费与协调器回归 `5 passed`，连续两次重建计两轮恢复、不创建进展事实。之后末次本机回归为相关扫描／正式运行 `70 passed`，仓库／工作区／依赖 `24 passed, 3 skipped`；Docker 全量和非 Docker 全量均长时间挂起后中止，未取得结果。不能将旧全量结果扩展为最后补丁的全量验证。

2026-11-05 Docker 恢复后 P2 最终全量：完整后端套件 `402 passed, 1 skipped in 460.62s`，本地报告 `.cache/p2-final-docker-ready-20261105.xml`（403 tests、0 failures、0 errors）；唯一跳过为需单独启用 `REPOWATCH_DOCKER_E2E=1` 的 `test_docker_object_handoff_end_to_end`，覆盖 LFS／子模块对象交接，不属扫描 P2。此前受 named pipe 阻塞的三个 Docker 用例复跑 `3 passed`。扫描／正式运行／调度专项 `76 passed`，Edge 扫描 UI、Ruff、TypeScript／Vite、OpenSpec strict 通过。P2 4.1—5.8 已有任务级证据并全部勾选；本阶段验收不包含 P3—P6、真实 GitHub 写入或最终产品验收。
