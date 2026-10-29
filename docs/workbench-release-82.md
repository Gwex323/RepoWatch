# 发布与服务恢复（#82）

更新：2026-10-29（Asia/Shanghai）。第五阶段在 `feature/workbench-release-82` 实施；基于第四阶段 PR #92 合并提交 `172c5a8`。复用现有 Issue #82，不新建重复事项。当前交付与验收状态见[验证记录](workbench-release-82-validation.md)。未在正式工作台启用、部署或执行合并。

## 权限与边界

- 自身维护仍默认停在 PR。后台不会自行扩大权限；提供本机所有者 CLI，合并与部署分别显式授权，WebUI 仅显示状态。
- 合并授权绑定一个精确 PR HEAD SHA；部署授权绑定一份完整清单摘要。改提交、路径、资产、端口或配置即产生新清单，必须重新授权。部署不隐含合并权限，合并也不隐含部署权限。
- 固定 `Gwex323/RepoWatch`、`Gwex323`、当前账号版本及仓库数字 ID。远程动作前重新验证身份和权限；撤销或账号变更禁止后续操作。监护不调用模型或 GitHub，只使用已经验证保存的本机授权。
- 合并命令要求 GitHub 返回同一 HEAD、实际默认分支、非草稿、可合并、CLEAN、独立 APPROVED 和成功的检查汇总；缺项保守拒绝。最终仍由 GitHub 规则执行合并。PUT 前保存意图，结果未知只 GET 核对，不重复发送合并请求。
- 部署要求 PR 已由 GitHub 确认合并，清单提交等于该 PR 的合并提交且位于实际默认分支历史中。不会直接 push 主干或本地绕过 PR 合并。

## 清单与运行绑定

清单保存在专用状态目录的 `manifests/<摘要>.json`，包含代码绝对路径、干净 Git 提交及源码摘要、选定 Python、前端逐文件摘要、原数据库绝对路径及稳定 ID/schema、监控配置引用及摘要、固定端口、账号绑定和上一清单。可选绑定维护任务，以及是否启动业务 workers。

受管入口使用清单解释器执行 `-I <目标源码>/scripts/managed.py`，在脚本中固定目标导入路径，并清除继承的 Python/RepoWatch 配置污染。不会依赖当前目录或旧 editable 安装选择应用。启动核对实际加载路径和解释器。

`config_ref` 指向所有者保护的本机 JSON 文件，仅接受 Langfuse 配置键。必须显式设置 `REPOWATCH_LANGFUSE_ENABLED` 为 `0` 或 `1`；启用时要求地址、公钥、密钥、SDK/追踪初始化可用。既有受管版本已启用监控时，后续版本不能静默关闭。清单、发布事件和 UI 不包含配置密钥；应以操作系统 ACL 保护配置、状态目录和数据库备份，不能把它们提交到 Git。监控初始化不等于远程 Langfuse 连通性验收。

## 发布事务与数据保护

1. 独占操作锁，验证精确授权、GitHub 合并事实、代码/资产/配置和上一运行绑定。初次接管若端口被未知服务占用，直接拒绝，不换端口、不按端口杀进程。
2. 先保存发布意图，再设置原库排空标志。API 拒绝新业务写入，执行、诊断、分析、修复、验收、巡检、Issue 同步、协调、维护和记忆评估停止领取新工作；已领取任务可完成。30 秒仍有活动任务则拒绝切换，不取消任务或清零额度。
3. 对原库执行 SQLite 一致性备份和完整性核验；只在备份的探测副本运行候选初始化。控制器使用不触发迁移的 `ExistingStore`，不能在备份前导入候选 `Store` 修改原库。
4. 比较原 schema 和原数据摘要：只允许兼容加法；保留业务记录、配置、预算、检查点、历史和原故障行。故障追加采用行水位，允许新增服务事件，不能删除或替换既有故障。会话及发布控制回执不参与业务数据等同性比较。
5. 再次校验权限及远程事实，持久化切换阶段后，在事务内复核空闲和数据未变，再停止身份匹配的旧进程、选择新清单、启动候选。
6. 就绪必须同时匹配版本、加载目录、解释器、PID、原库路径/ID、配置摘要和监控模式；还通过 HTTP 校验实际返回的 HTML/资产字节。HTTP 200 或磁盘版本标签均不充分。
7. 校验原数据保留后记录部署事实。先持久化 `completed` 收尾标记，再提交解除排空，最后删除意图；中断后仅幂等完成收尾，不重放发布，也不回滚已放行的业务。

运行检查接口 `/api/release-health` 仅接受启动生成的私有随机令牌，普通请求返回 404；详细状态 API 仍要求工作台登录。不存在面向模型的宿主任意命令入口。

## 监护、中断与回滚

- `supervise` 是独立隐藏进程，Web 退出或模型离线不影响其循环。单监护锁和操作锁防止竞争；只停止 PID、创建时间、实际可执行文件和命令行全部匹配的进程。
- 10 分钟最多预留 3 次重启，计数在独立 SQLite 中持久化，监护重启不刷新额度。固定端口占用、身份改变、未知启动回执或预算耗尽均明确受阻，不误杀其他进程。
- 重启意图先于排空标志持久化；成功后先提交清排空，再删除重启意图。启动回执尚无 PID 时，只有扫描绑定解释器/命令/随机令牌未发现存活进程且端口空闲，才可证明未启动并清理；无法证明就停止。
- 服务事件先保存到独立 `journal.db`。原库不可用时仍保留失败记录；恢复后以稳定事件 ID 幂等入账，再确认本地队列，不重复计数或伪装成功。
- 候选失败可以切回上一已授权、已验证清单的代码和资产，但**不会自动恢复旧数据库备份**。回滚要求当前 schema 与旧清单已知 schema 完全一致；即使只是新增字段，也要求人工评估，不推断旧程序可兼容。
- 回滚保留部署后新增数据和失败历史；没有上一清单、schema 不明或身份不明时保持受阻和排空，由所有者处理。备份只是恢复依据，不是自动覆盖新数据的许可。
- 控制进程中断后先运行 `reconcile`。旧版本恢复与手动回滚中断共用空闲保护；不会为解除排空而终止活动任务。候选版本只有通过原始数据和 schema 证据核对才补记部署；未完成收尾不会盲目重发操作。

## 所有者操作

以下是手动模板，不是本次交付已经执行的正式操作。所有路径需替换为已核对的绝对路径；`$entry` 必须来自已合并的干净目标源码，`$python` 是已安装运行依赖（含 `psutil`）的选定解释器。数据库必须是既有工作台原库，不能用空库替代。首次从非受管服务切换前，先按已确认进程身份停止原服务；不会自动接管任意监听进程。

```powershell
& $python -I $entry init --database $originalDatabase
& $python -I $entry plan --code-root $checkout --python $python --database $originalDatabase --config-ref $privateConfig --state-root $releaseState --port 8765 --pr $mergedPr
# plan 仅生成未授权清单；从输出核对 $manifest 和精确 $manifestId。
& $python -I $entry authorize --database $originalDatabase --kind deploy --target $manifestId
& $python -I $entry deploy --manifest $manifest
# 部署成功后启动独立监护，或首次使用 scripts/start.ps1 -Manifest ... -Background。
& $python -I $entry supervise --state-root $releaseState
```

初次接入 `init` 仅加法初始化发布台账及原库 ID；停止旧服务、备份和原路径核对应由所有者先完成。后续候选迁移仍在发布备份之后运行。`scripts/start.ps1 -Manifest <绝对路径> -Background` 委托受管入口先发布再隐藏启动监护；不传 `-Manifest` 的原开发启动模式保持不变，**不具备固定清单、固定端口和发布恢复保证**。

```powershell
& $python -I $entry reconcile --manifest $interruptedManifest
& $python -I $entry rollback --manifest $currentManifest
& $python -I $entry revoke --database $originalDatabase --kind deploy --target $manifestId
# 仅在所有者明确选择辅助合并且 GitHub 全部规则满足时：
& $python -I $entry authorize --database $originalDatabase --kind merge --target $exactHeadSha
& $python -I $entry merge --database $originalDatabase --pr $pr --head $exactHeadSha --state-root $releaseState
```

若允许部署后续办受影响原任务，生成清单时还需 `--maintenance-task <原维护任务ID>`，并在该精确清单授权上显式指定 `--resume-original`。保持维护开关、来源依据及原任务剩余额度有效，部署且就绪后才调用既有分析/修复/验收 retry 适配器。执行续用真实检查点和累计预算，不创建替代任务、不自动确认新发现；撤销部署授权或账号变化会被后续执行权检查阻止。仅原来源步骤真实通过，故障采集器才能标记恢复；发布成功本身绝不解决原故障。

## 后续阶段

第六阶段 #83 负责跨阶段完整链路和逐步启用。本阶段不宣称真实维护 PR 已被自动合并、正式库已迁移、真实 Langfuse 连通、原生产故障已修复，亦不代替 Linux 与真实模型验收。整个 OpenSpec 变更继续保持未归档。
