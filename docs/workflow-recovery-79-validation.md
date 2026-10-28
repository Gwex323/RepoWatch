# 第二阶段验证记录（Issue #79）

日期：2026-10-28。分支：`feature/workflow-recovery-79`，基础：`ba043b1`。
本记录只覆盖规则恢复；不代表中央模型调度、自身维护、合并或部署已完成。

## 验证结果

最终补丁后端按文件名排序分为三个互不重叠的组（文件序号 `% 3`），全部通过。
JUnit XML 按区分大小写的用例身份核对，共 1100 个不同用例，无跨组重复。

| 检查 | 通过 | 失败 | 跳过 |
| --- | ---: | ---: | ---: |
| 后端第 0 组 | 355 | 0 | 0 |
| 后端第 1 组 | 370 | 0 | 12 |
| 后端第 2 组 | 363 | 0 | 0 |
| 后端合计 | 1088 | 0 | 12 |
| 恢复与工具参数定向回归（包含在全量中） | 55 | 0 | 0 |
| Playwright 系统故障页面 | 4 | 0 | 0 |

前端 `npm run build`（TypeScript + Vite）、`python -m ruff check repowatch tests`
和 `git diff --check` 通过。真实 Docker 集成的 12 个用例未显式启用，故跳过。

- 新增核心用例已覆盖 SQLite 双连接竞争、稳定动作重放、过期租约 fencing、
  跨流程预算、历史计数导入、默认关闭、退避、配置版本及权限保护。
- 原生 Agents SDK 用例验证两条范围调用实际执行一次、两份有序真实回执；
  数据库用例验证路径非法、冲突、空批次、超量、预演回滚和范围版本竞争。
- 真实 Repairs 服务入口用例覆盖排队后模型、账号、范围变更、关闭开关、
  崩溃回执结算后续办、额度耗尽落地失败和旧产物写回拒绝。
- 原分析、验收、审查反馈、远程 mutation intent 的完整后端回归通过。
- 前端 TypeScript 检查和 Vite 构建通过；浏览器 4 项通过，覆盖默认关闭、持久化、
  关闭后任务不变、故障过滤与关闭、来源跳转、移动端宽度及陈旧详情隔离。
- 定向回归 55 项通过（恢复机制及分析工具参数）；全库 Ruff 和 `git diff --check` 通过。

本表只记录最终补丁重跑结果，不将中间失败算作通过。本机原始报告位于忽略的
`data/qa-79/final-shard-{0,1,2}.xml` 和 `data/qa-79/recovery-final.xml`。

复现命令（PowerShell，在项目虚拟环境中执行）：

```powershell
$files = @(Get-ChildItem tests -Filter 'test_*.py' | Sort-Object Name)
foreach ($shard in 0..2) {
    $selected = @(for ($i = 0; $i -lt $files.Count; $i++) {
        if ($i % 3 -eq $shard) { $files[$i].FullName }
    })
    python -m pytest -q --tb=short --junitxml="data/qa-79/final-shard-$shard.xml" @selected
}
python -m pytest -q tests/test_workflow_recovery.py tests/test_analysis_tool_arguments.py
python -m ruff check repowatch tests
npm run build
```

浏览器服务在独立终端运行 `tests/e2e/fault_server.py`，设置
`REPOWATCH_FAULT_TEST_PORT=8789`；另一终端设置
`REPOWATCH_TEST_URL=http://127.0.0.1:8789` 后运行
`npm run test:e2e -- tests/e2e/system-faults.spec.ts`。
每轮完整浏览器运行前重启该临时服务，不能复用已被关闭或补录操作改变的测试数据。

## 验收映射

| 场景 | 证据 |
| --- | --- |
| 双连接竞争、稳定身份、租约过期、旧产物拒绝 | `tests/test_workflow_recovery.py` |
| 跨流程预算、历史计数导入、迟到令牌去重、时间与续办耗尽 | `tests/test_workflow_recovery.py` |
| 双调用原生 SDK、整批拒绝、预演回滚、重放与版本竞争 | `tests/test_workflow_recovery.py`、`tests/test_analysis_tool_arguments.py` |
| 分析超时、参数、交接及检查点续办 | `tests/test_analysis_auto_recovery.py`、`tests/test_analysis_handoff_recovery.py`、`tests/test_analysis_report_recovery.py` |
| 修复输出恢复、独立再审、真实验收失败反馈 | `tests/test_repair_output_recovery.py`、`tests/test_repair_review_feedback.py`、`tests/test_acceptance_recovery.py` |
| 依据变化、取消、发布门禁与未知结果先核对 | 原分析、修复、验收全量回归及 `tests/test_github_contributions.py` |
| 登录、CSRF、配置版本、默认关闭、开关回退 | `tests/test_workflow_recovery.py`、`tests/e2e/system-faults.spec.ts` |

原生 SDK 用例实际经过工具执行与真实 SQLite 回执；模型、GitHub 发布等场景使用
替代模型和远程模拟，不把模拟通过解释为真实供应商或目标仓库写入验收。
浏览器使用临时数据库、关闭业务 worker，未改动正式任务。截图保存在
`data/qa-78/faults-desktop.png` 和 `data/qa-78/faults-mobile.png`（沿用已有用例路径）。

## 独立 Review

首轮审查未通过，发现并修正：过期 worker 产物覆盖、排队后的依据失效、崩溃租约
结算、claim 内额度异常回滚和根任务时间上限。复审又补充耗时耗尽领取、迟到令牌
入账和历史失败扫描饥饿用例。独立只读最终复审通过，未发现必须阻止 PR 的新缺陷。
后续仅复用现有复选框样式和更新文档，审查者再次确认通过结论有效。
代码 review 与运行验证分开：review 未运行完整测试，全量结果由本记录单独提供。

## 限制

没有真实付费模型联调、目标仓库写入或生产部署。本次未启用真实 Docker 集成，
不沿用 #88 的 Docker 结果冒充本次验收。当前操作系统为 Windows，Linux、其他
供应商和实际账单核验仍不在本次验证范围。
