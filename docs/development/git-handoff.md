# Windows→Linux Git 对象交接（1.6）

日期：2026-11-04。范围：P0 的 E11／E15 关键切片，验证 Windows Git 对象→Linux 容器工作树→候选对象回传及内容对账。它不是生产工作区、发布器或完整 E11／E15 验收。

## 目标与边界

- 宿主只创建裸对象和 bundle，不检出用户日常仓库；夹具位于临时目录，不读取或改写用户工作区。
- bundle 通过 `docker cp` 进入容器根文件系统，再复制到命名卷；容器不挂载宿主目录、不传凭据环境变量、使用 `--network none`。
- 容器内从 bundle 克隆并物化工作树，核对内容后修改文件、提交候选并导出候选 bundle。
- 宿主把候选 bundle 导入独立裸仓库，核对树、文件内容和对象完整性；不自动推送。
- LFS 指针和子模块 gitlink 单独登记；本轮不提供 LFS 实体或子模块对象，因此交付明确阻塞，不能把 Git 对象交接成功当成全部内容可用。

## 运行

前置条件：Windows 上有 Git，Docker Linux engine 可用，本地已有 `alpine/git:latest` 镜像。

```powershell
backend/.venv/Scripts/python.exe experiments/run.py git-handoff-contracts
backend/.venv/Scripts/python.exe experiments/run.py git-handoff
```

第一条运行不依赖 Docker 的契约测试；第二条执行真实容器交接，报告写入 `experiments/results/local/e11-<时间>.json`。真实实验退出码 `0` 只表示切片检查通过，不表示 `delivery.ready=true`。

## 夹具与对账

夹具用 `git hash-object`／`update-index --cacheinfo` 直接在索引中建立对象，避免 Windows 文件系统无法同时表示大小写冲突、符号链接或可执行位：

| 路径 | 模式／内容 |
| --- | --- |
| `lf.txt` | LF 文本 |
| `crlf.txt` | CRLF 字节 |
| `Dir/File.txt`、`Dir/file.txt` | 仅大小写不同且内容不同 |
| `run.sh` | `100755`，检查容器内可执行位 |
| `link.txt` | `120000`，指向 `lf.txt` |
| `large.bin` | 标准 LFS 指针；无实体 |
| `vendor/lib`、`.gitmodules` | `160000` gitlink；无子模块对象 |
| `tracked.txt` | 容器内追加内容并提交为候选 |

容器清单逐项记录模式、blob、实际 SHA-256、大小、链接目标、LFS 指针标记和 gitlink 对象可得性。宿主将清单与预期对象比较，并把候选 bundle 导入新裸仓库核对树和 `tracked.txt` 内容。

## 当前结果

2026-11-04 真实报告 `experiments/results/local/e11-20261103T164142737329Z.json`：

- `status=passed`，容器退出码 `0`，耗时 7.297 秒；镜像 `alpine/git@sha256:ac6c828f...`，容器内 Git 2.54.0。
- 15 项检查全部通过：路径、模式、blob、内容、LF／CRLF、大小写并存、可执行位、符号链接、LFS 指针识别、gitlink 记录、夹具提交／树、候选树、候选内容及候选对象完整性。
- `delivery.ready=false`，阻塞项为 `lfs_entity_missing:large.bin` 和 `submodule_object_missing:vendor/lib`；符合“缺内容不放行”。
- 隔离记录：`network=none`、无宿主挂载、`cap-drop=ALL`、`no-new-privileges`、无凭据环境变量。

## 限制

- 本轮只验证负向 LFS／子模块可得性；没有实现 LFS 实体传输、子模块 fetch 或受控上传适配，真实内容可用时仍需另行验证。
- 没有测试宿主 checkout 的换行转换或脏工作区；对账对象是 Git 对象与容器实际物化内容。
- 容器以 root 运行，隔离参数是 P0 切片，不等于生产安全边界；完整 E11 的启动、会话、来源校验和凭据边界仍待后续任务。
- 不证明发布器可推送、远端接收方可取得外部内容，也不能替代完整 E15 扫描与证据失效规则。
