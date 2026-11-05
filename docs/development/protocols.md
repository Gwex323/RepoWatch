# P0 运行协议 v1

日期：2026-11-03。字段定义以 [`protocols.py`](../../backend/src/repowatch/domain/protocols.py) 为准；跨模块行为边界沿用[实施契约](../implementation-contracts.md)。本页说明 P0 已实现的结构校验，不代表 P1 的运行权限、P3 的真实证据验收或受控发布已完成。

## 任务与结果

`TaskEnvelope`、`StructuredResult`、`EvidencePackage` 的 `protocol_version` 固定为 `"1"`；未知版本、未知字段、空标识、非正修订及非法 Git 对象／SHA-256 摘要会被拒绝。对象为不可变 Pydantic 模型，集合使用 tuple。

`RunContext` 包括仓库、主任务、运行、输入／验收修订、配置快照及修订、执行代次，以及 base、候选提交、Git 树和实际内容清单摘要。`validate_result` 将返回结果与授权信封逐项比较；产物引用也须匹配完整上下文。结果的 `completed` 仅表示角色已返回结构化内容，业务完成另由对应阶段核验。

信封还包含角色、模式、目标、预算归属／账本修订／预留标识、工具策略和记忆修订引用。子任务使用独立运行标识及 `parent_run_id`，共享预算归属；预算引用的结构合法不等于授权仍然有效，P1 必须从账本核对。

## 模式工具策略

| 模式 | 最大可授予能力 |
| --- | --- |
| scan | 读源码、写独立分析、请求 Runner 检查 |
| investigate | 读源码／证据、写独立分析、请求 Runner 检查 |
| plan | 读源码／证据、写独立分析 |
| reproduce | 读源码／证据、写独立分析、请求 Runner 检查 |
| implement | 读源码／证据、写当前任务源码、请求 Runner 检查 |
| review | 读源码／证据、请求 Runner 检查 |
| distill | 读证据 |
| diagnose | 读诊断证据、请求有界检查 |

实际授权可进一步缩小；策略版本固定为 `"1"`。独审角色与 review 模式双向绑定。发布、改业务验收、宿主 shell 和凭据读取均不属于模型工具。`request_check` 是请求受控 Runner，不赋予直接写终端。P0 的策略校验不能代替 P2 的容器、路径与进程隔离。

## 证据结构

`ArtifactReference` 包含归属、种类、归档内相对路径、摘要和字节数；拒绝绝对路径、盘符、反斜线及 `..`。实际文件完整落盘、链接防护和摘要回读在产物存储适配中实施。

`EvidencePackage` 要求冻结候选、检查记录、产物及独立 `review_context`／审查产物。检查记录区分 base、previous_head、candidate，保存实际提交／树、环境摘要、命令、退出码、结果和日志／探针引用；候选记录必须匹配信封候选及树。审查可使用独立运行及不同的显式模型配置快照，其任务、输入、验收、代次与代码上下文保持匹配。

结构有效的证据允许保存 `unable`、失败或不适用结果，以便准确记录障碍。结构校验不判定探针是否有效、前失败后通过、回归未恶化或独审结论通过；这些仍由 P3 规则实现。previous_head 的实际归属与前后检查环境一致性也由后续验证器核对。

## 验证入口

在 `backend/` 使用锁定 Python 3.12 环境运行 `python -m pytest tests/test_protocols.py -q`。覆盖合法 JSON 往返、错任务／运行／配置／输入／代次、错代码版本、越权工具、归档路径、产物归属及候选／独审引用。P0 结果见 [P0 报告](../../experiments/results/p0.md)。

P1 的 `test_formal_runtime.py` 将相同协议接入实际 SDK HTTP 夹具和正式 Deep Agents 图：首步预算引用是真实预留，模型／命名子任务固定角色快照，交接重新核对完整上下文和实际产物。未知／越权工具不执行，旧代次不能写回；宿主核验结果与业务／checkpoint 确认分离，模型自述不构成任务验收。复跑及当前证据见[正式运行引擎](runtime.md#复跑与实验映射)。
