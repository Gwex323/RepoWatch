# 第四阶段验证记录（#81）

日期：2026-10-29（Asia/Shanghai）。基线：第三阶段 PR #91 合并提交 `74b0918`；分支：`feature/system-maintenance-81`。复用既有中文 Issue #81，其根因、范围和验收正好覆盖第四阶段，不新建重复 Issue。开始实施前已同步实际默认分支 `codex/initial-upload`，使用独立 worktree 保留原工作区未提交内容。

## 验证范围

按所有者要求不运行全量回归，只进行维护链路的定向检查。无正式数据库、付费模型、真实维护仓库写入、真实 Docker 验收、生产部署或主干直接写入。

- 后端：`python -m pytest -q tests/test_maintenance.py tests/test_github_contributions.py::test_unresolved_post_intent_never_blindly_replays`，13 passed（12 个维护用例、1 个共享发布器回归），16.27 秒。
- 前端：`npm run build`，TypeScript 与 Vite 构建通过。
- 静态检查：本次所有修改 Python 文件 Ruff 通过。
- 浏览器：仅 `tests/e2e/maintenance.spec.ts`，1 passed；桌面 1440×1080、移动 390×844。核对固定维护范围、默认关闭、模型未就绪禁用、无 JS 错误及无横向溢出，已查看截图。使用临时库和无 worker 夹具服务。

## 验收映射

| 任务 | 证据 |
| --- | --- |
| 4.1 专用任务与分类 | 默认关闭、非内部分类和类别排除；真实 SDK 严格分类输出，无虚假发现/确认 |
| 4.2 绑定与权限 | 错账号、错仓库、无 push 权限均不能启用；准备补丁前再次校验 |
| 4.3 查重与远程核对 | 人工 Issue/PR 根因证据复用并补充评论；标题相似不复用；Issue 响应丢失只核对，不重复 POST |
| 4.4 隔离 worktree | 真实本地 Git 夹具，实际默认分支与 feature 分支；已有脏目录拒绝覆盖，用户改动保留 |
| 4.5 验证与独立反馈 | 真实 SDK 的拒绝→新补丁→重新验证→再审；验证失败或批准后树被改动均被 gate 拒绝 |
| 4.6 中文发布 | GitHub REST 替身检查五部分及 Issue 引用；丢失 PR 响应的发布重试只有一次 POST；分支被改动阻止新 PR |
| 4.7 文档与 review | 本说明、维护策略、README、总设计和实施计划同步；独立 review 通过，中文交付 PR 待创建 |

完整正向用例使用真实 SQLite、真实 Agents SDK runner/工具/原生交接、真实 RepairWorkspace 和替代 Docker/GitHub：内部证据分类→中文 Issue→隔离候选→第一次独立拒绝→重新测试→再审批准→中文 PR。Git fetch/worktree 本身另用本地测试仓库验证，未把本地 file 协议带入生产 Git 适配器。

PR 合并模拟只得到“待部署验证”，故障仍为 blocked；没有调用合并 API。来源分析非预算停止原因可使用剩余额度，32 次上限不被绕过；授权撤回中断等待中的分类并禁止建单。

## 独立 Review

`coordinator_review` 只读 review 及增量复核均通过，无剩余 P1/P2。已修正：

- 人工历史工作不能只因缺少自动标记而被遗漏；关联候选先于 POST 核对。
- 分析原流程不可自动续办与真正预算耗尽分开，仍保留取消和硬限。
- complete 分支回执不替代当前 SHA 校验；PR POST 前及返回后均绑定已批准提交。
- 复用 PR 的会话评论核对 `/pull/{n}#issuecomment-...`，不误用 Issue URL。

review 另用一个全替身/临时库场景复现分支改动漏洞，未运行全量测试或访问正式库。最终增量复核确认源码窗口、绝对行号、prepare 权限和 UI 能力提示没有绕过服务端 gate。

## 剩余边界

真实模型、真实维护写入、Docker 依赖安装、Linux 和部署均未验证。维护自动补丁仅覆盖 Python 实现及新增 Python 回归；其他语言和受保护文件拒绝。部分源码安全筛查、来源额度或已过期限会使任务受阻，这是停止门禁，不伪装完成。阶段五、六未实施，OpenSpec 整体不归档。
