# Issue #114 源码安全误报修复与独立审查记录

- 日期：2026-10-31（Asia/Shanghai）。
- Issue：<https://github.com/Gwex323/RepoWatch/issues/114>。
- 分支：`feature/source-safety-sk-prefix-114`，从最新默认分支
  `origin/codex/initial-upload` 的 `caa53fa` 创建。
- 结论：代码、测试和真实 `deepseek-flash` 独立审查已通过，可以提交中文 PR；
  尚未合并、部署或完成 `gateway-model-assistant` 目标 PR 验收。

## 真实失败

Issue #108 的隔离验收中，修复任务 `repair_67b89cfeabab4ce9` 无法读取
`tests/test_trae_adapter.py`：

- 第 35 行包含模型名称 `volcengine-plan//sk-only`；
- 第 38 行包含占位值 `sk-fallback-key`；
- 通用日志脱敏规则把任意 `sk-` 连续字串都当作凭据，导致整个文件在读取阶段
  以 `credential_pattern` 失败；
- v4pro 后续提交的新回归文件第 11 行又被 `credential_literal` 拒绝，但补丁在
  落库前已被丢弃，因此没有保留该文件内容，不能反推或伪造具体代码。

任务最终为 `repair_sensitive_content`，未进入测试、独立审查或 PR 发布。

## 修复

1. `redact_known` 增加仅由源码筛查启用的 `source_screening` 模式。
2. 日志和事件脱敏继续使用原 `sk-` 规则，避免改变现有日志安全语义。
3. 源码模式要求 `sk-` 候选具有非标识符左边界，且后缀至少 20 个
   key-shaped 字符；短模型标签和占位值不再误报。
4. GitHub token 分支保持与日志规则相同的格式；配置密钥、凭据赋值、URL
   userinfo 和长 `sk-` 密钥仍走原有阻断路径。
5. 回归覆盖 19/20/21 字符边界、左边界、Unicode 分隔符、注释、
   Markdown/JSON、真实 `RepairWorkspace` 读取与补丁准备，以及长
   `sk-`/GitHub token/凭据赋值在 `prepare` 阶段继续拒绝。

## 模型与审查

- 修复初稿由 `deepseek-flash` 生成，但初稿包含未解析的 `{min_len}`，且先替换
  长密钥再调用原日志脱敏，仍会误报短 `sk-`；主执行路径没有直接采用该错误。
- 第一轮独立审查不通过，指出阈值、GitHub 分支不一致、调用路径测试和边界
  测试不足。有效意见已修正；“移除左侧边界”会重新引入子串误报，未采纳。
- 第二轮独立审查不通过，补充了 `prepare` 拒绝、Unicode 分隔符、注释和文档。
- 第三轮独立审查通过，剩余两项均为 P3 残余风险，没有必须修改项：
  - 20 字符阈值可能漏掉不在凭据赋值上下文中的更短真实 `sk-` 值；
  - `KNOWN_SECRET` 与 `SOURCE_KNOWN_SECRET` 仍由两条分支维护。

审查证据：

- `.codex-backups/flash-review-source-safety-114.json`：第一轮，不通过，5 项。
- `.codex-backups/flash-review-source-safety-114-r2.json`：第二轮，不通过，4 项。
- `.codex-backups/flash-review-source-safety-114-r3.json`：最终通过，2 项 P3。

## 验证

- `tests/test_source_safety.py` 加真实读取/补丁准备回归：51 passed。
- `tests/test_diagnosis.py` 与 `tests/test_repairs.py` 联合：213 passed、2 failed。
- 两项失败在未包含本次修改的干净 HEAD 归档中同样复现：
  - verification 键顺序/集合断言；
  - `too_large` 场景未产生 `source_unavailable` 事件。
- `ruff check repowatch/diagnosis.py repowatch/source_safety.py
  tests/test_source_safety.py`：通过。
- `git diff --check`：通过。

## 未完成范围

- 尚未合并 PR，尚未重启隔离工作台载入该修复。
- 尚未重新执行 `gateway-model-assistant` 的真实分析、修复、Flash 审查和
  中文目标 PR 发布。
- 本修复不是通用凭据分类器；短 `sk-` 值的残余风险已记录，不能据此宣称所有
  供应商密钥格式均已覆盖。
