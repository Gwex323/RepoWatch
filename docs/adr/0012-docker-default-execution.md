---
status: accepted
---

# 仓库代码默认在 Docker Linux 环境中执行

用户选择将仓库命令和 Agent 文件操作默认放在 Docker Linux 环境中，Windows 使用 WSL2 后端，本机 API／Worker 负责协调。该选择接受 Docker／WSL 的安装和生命周期管理成本，以统一任务环境和宿主边界；必须依赖宿主能力的项目通过显式启用的原生执行适配处理。
