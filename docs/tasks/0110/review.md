# Issue #110 修复与独立审查记录

- 日期：2026-10-31（Asia/Shanghai）。
- Issue：<https://github.com/Gwex323/RepoWatch/issues/110>。
- 分支：`feature/scope-read-recovery-110`，从最新实际默认分支
  `origin/codex/initial-upload` 的 `5c25840` 创建。
- 结论：代码修复和真实 flash 独立审查已通过，可以提交中文 PR；真实
  `gateway-model-assistant` 端到端验收仍待组合验证，不能提前宣称完成。

## 真实失败

在 Issue #108 的隔离验收中，分析
`analysis_0cb02e0b1c7148ee`（revision
`9870a74369a81c53f9f4edb384598b240a16cbc0`）的四个分析角色实际使用
`deepseek-flash` 越过恢复身份错误，但随后失败为
`analysis_scope_read_not_followed`。事件显示：

- `required=scope`；
- `calls=1`、`matching=0`、`other=1`、`allowed=2`；
- `recoverable=false`、`recovery=request_mismatch`、
  `corrections_remaining=0`。

错误工具没有执行，也没有发生范围登记、修复、验证或发布。一次纯错误工具批次
不应直接耗尽整个真实分析，但也不能放宽混合批次或权限边界。

## 修复

1. 只有 `required=register_task_scope`、`reason=wrong_tool`、
   `matching=0`、`tool_repair is None` 且尚未纠正时，才允许一次纠正。
2. 纠正请求只说明没有工具执行，并要求重新返回真实
   `register_task_scope` 调用；不包含错误工具名或参数。
3. 第二次错误工具、混合正确/错误工具、source 错误工具、超批次、拒绝和参数
   修复路径继续失败关闭。
4. 原有缺失调用路径的纠正文案和调用次数保持不变，避免扩大无关行为变化。
5. 新增成功恢复、二次失败、source 纯错误、scope 混合、scope 超批次和拒绝
   六类边界测试，并验证敏感名称与参数不进入事件。

## 模型交接

- 修复方案由真实 `deepseek-v4-pro` 生成，模型调用消耗 1840 tokens；初稿的恢复
  分类和测试不完整，主执行路径按实际代码结构收窄实现。
- 老 v4pro 子执行者仅返回待命，没有产生文件修改；本次没有把该空结果算作实现。
- `deepseek-flash` 第一轮审查不通过，指出空响应路径被意外改变、纠正文案影响
  无关路径、混合/非范围错误工具缺少测试。上述问题均已修正。
- `deepseek-flash` 第二轮审查不通过，指出二次失败测试的总调用数容易被误读。
  已改为直接断言 `register_task_scope` 的尝试次数，并补齐超批次与拒绝负向测试。
- `deepseek-flash` 第三轮审查通过。残余 P3 仅为“两次 retry 赋值可合并”的可读性
  提示，不阻断合并。

真实审查证据保存在本地隔离目录
`.codex-backups/flash-review-scope-110.json`；已记录模型、时间、diff 摘要、
token 用量和逐项发现。

## 验证

- 与修复直接相关的 6 个 nodeid：6 passed。
- 完整 `tests/test_analysis_prerequisites.py`：23 passed、1 failed。
- 唯一失败为基线 `5c25840` 已有的
  `test_explicit_closing_with_missing_execution_never_calls_provider`：
  测试向 `StageModel` 传入不存在的 `force_closing` 构造参数；本次没有改变该
  构造器或测试，不能算作本次通过或本次引入。
- `ruff check`、`compileall`、`git diff --check`：通过。

## 未完成范围

- 尚未提交、推送或创建 Issue #110 的中文 PR。
- 尚未把 Issue #108 和 #110 两个未合并 feature 分支组合到隔离实例执行新的真实
  分析；只有在组合验证中才允许继续观察 flash 到 v4pro 再到 flash 的完整链路。
- 不得自动合并或部署任何 PR。
