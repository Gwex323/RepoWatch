# 0063：整合工作台任务范围与报告恢复

- 关联：Issue #63，承接 #59 / PR #60 和 #61 / PR #62。
- 基于最新主干 51be49f 建立 feature/workbench-integration-63。

## 设计与冲突处理

同时保留任务范围面板和报告恢复提示、范围工具和内部检查点事件、逐项验收审查和
报告提交前的依据校验。报告完成仍调用 save_scope_review，不绕过范围版本和证据检查。
继续分析仍按已有 finding 身份去重，保留人工状态；摘要变量使用 finding_digest，
避免与检查点 digest 函数混淆。

模型事件包装返回原回调的结果，使 register_task_scope 能获得服务端范围包并更新
分析上下文；检查点保存对应的当前范围与模型身份。恢复过程继续关闭工具，只有真实
读取和交接记录可用于审查，旧任务无检查点时执行完整流程。

## Review 记录

静态自审覆盖：范围登记回调返回值、独立审查 schema、完整性落库、既有 finding 去重、
检查点保存及失效、恢复入口、前端类型与两个面板的同时展示。修正事件包装丢失返回值，
保留所有范围与报告依据门槛。未自动合并主干。

## 验证与部署

Ruff、compileall、TypeScript、Vite 构建及 git diff --check 通过。
本次未新增或运行自动化测试，未调用真实模型或发布目标仓库内容。

更新前使用 SQLite backup API 备份生产数据库，并在独立副本初始化新服务：
37 张原有表的行数与内容摘要全部一致，integrity_check 和 foreign_key_check 通过。
新增 analysis_review_checkpoints 表；目标分析保持 failed，六项任务范围完整，
旧记录未伪造可恢复标记。部署后再次检查原表内容与服务入口。
