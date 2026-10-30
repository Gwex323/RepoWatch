# ADR 0075: flash 监控与 v4pro 修复的模型升级编排

状态：已实现，默认关闭，尚未部署。

## 背景

RepoWatch 已支持按角色选择供应商与模型，但缺少一条可审计的受控升级路径，明确保证低成本
flash 模型持续负责观察、分析、审查和发布协调，只有服务端确认需要修改代码时才调用 v4pro，
并在修复阶段结束后回到 flash。

## 决策

### 加法单例策略，默认关闭

新增 `model_escalation_policy` 单例表：

- `enabled`：默认 `0`，关闭时完全沿用 `agent_model_settings.routes`。
- `version`：乐观并发版本，每次保存递增。
- `monitor_route` / `repair_route`：JSON 对象，仅包含 `provider_id` 和 `model`。
- `updated`：最近一次保存时间。

策略不会改写供应商、角色选择、密钥或历史任务。停用后立即恢复原角色路由。

### 从现有目录解析，不写死模型名

启用时调用 `ModelProviders.resolve` 校验两条路由，并要求供应商存在、模型存在且已保存加密凭据。
任一条件不满足时返回 `model_provider_missing`、`model_provider_model_missing` 或
`analysis_model_key_required`，不隐式回退到其他模型。

### 六角色分流

- 监控模型：`system_coordinator`、`repository_analyst`、`problem_investigator`、
  `reproduction_assistant`、`independent_reviewer`。
- 修复模型：仅 `repair_agent`，且仅当 flow 为 `repair` 且当前存在已授权、运行中的持久化
  修复任务。
- `classify_maintenance` 明确使用 `force_monitor`，不会因复用 `repair` flow 而误升级。
- 修复授权失效时失败关闭：不会把 `repair_agent` 降级到监控模型继续修改代码；
  仅维护分类可显式使用 `force_monitor`。

### 阶段不可变快照

每次进入阶段时，`AnalysisModel._run_models` 在同一事务中冻结实际供应商、模型、接口、推理强度、
凭据依据摘要和策略版本。阶段执行期间保存策略或供应商只影响后续阶段。

### 触发与返回

升级触发由 `repairs.py` 在通过现有租约、来源资格、当前授权、预算和账号检查后，将
`_repair_task_id`、`_analysis_id`、`_finding_id` 和 `_model_escalation_authorized` 写入内存上下文。
`ModelEscalation.repair_authorized` 只接受运行中的修复任务；模型建议、错误文本和单一异常不能触发。
修复完成、失败或受阻后，`_run_models` 记录 `model_escalation_return`，后续协调与独立审查继续使用监控模型。
该内存授权标记不会写入任务上下文；进程重启或任务重新领取时必须重新通过现有门禁。

### 权限与预算连续

模型升级不创建第二个恢复根、不重置累计用量、不新增工具或发布权限。GitHub Issue/PR/合并/部署门禁保持不变。

### 只读审计

`/api/model-escalation` 返回启用状态、两条路由、就绪情况和缺失条件，不返回密钥或密文。
运行事件记录 `model_escalation_policy`、`model_escalation_upgraded`、`model_escalation_return`；
旧任务没有可靠依据时前端显示未知，不按当前策略倒推历史调用。

## 回滚

1. 调用 `POST /api/model-escalation`，`enabled=false`，保留原路由版本递增。
2. 运行中的阶段继续使用已冻结快照；新阶段恢复 `agent_model_settings.routes`。
3. 保留 `model_escalation_policy` 和运行事件，不删除任务、预算或远程回执。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_model_escalation.py -q
.\.venv\Scripts\ruff.exe check repowatch tests/test_model_escalation.py
.\.venv\Scripts\python.exe -m compileall -q repowatch
npm.cmd run build
```

本策略不自动合并、部署或创建 PR；目标仓库和 RepoWatch 的 PR 仍需独立 review 与授权。
