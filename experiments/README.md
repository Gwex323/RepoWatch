# 可复现实验

实验定义及阶段门槛以[可行性报告 §9](../docs/feasibility-report.md#9-必须通过的可行性实验)和 [OpenSpec tasks](../openspec/changes/build-github-issue-agent-harness/tasks.md)为准。

P0 实验使用后端锁定环境运行；真实供应商、Docker 或编码资产缺失时必须记录未通过。测试夹具与模拟端点不能替代真实环境验收。脱敏、可复核报告保存在 `results/`；设备私有运行数据保存在已忽略的 `results/local/`，不得保存密钥。

从项目根目录执行 `backend/.venv/Scripts/python.exe experiments/run.py p0-local`，或将套件改为 `bootstrap`、`protocols`、`e7`、`runtime`；入口执行实际 pytest 并保存退出码和日志，失败返回非零。环境准备见[开发入口](../docs/development/README.md)，本次结果见 [P0 报告](results/p0.md)。

E1：`e1-contracts` 仅执行本地 HTTP 夹具；`e1` 按明确配置执行两家真实供应商的 8 项矩阵，缺配置／凭据时返回未通过及零调用。模板为 `providers.example.json`，本地配置为已忽略的 `providers.local.json`；填写及消耗说明见 [E1 开发说明](../docs/development/e1-providers.md)。
