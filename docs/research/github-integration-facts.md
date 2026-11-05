# GitHub 接入与 PR 检查事实

调查日期：2026-11-02。仅查 GitHub 官方滚动文档，不访问凭据或私有仓库，不执行写操作；以下事实支持接入设计，不能视为端到端权限或 CI 实测结果。

当前身份与轮询选择见[接入设计](../integration-discussion.md)，按后续用户调整的 2 小时／每天一次频率及其运行约束见[实施契约](../implementation-contracts.md)。本文的官方接口事实不另行定义产品调度规则。

## 认证边界

- Fine-grained PAT 限于资源所有者及所选权限，当前文档列出的限制包括向用户不是成员的公共仓库贡献，以及部分 outside collaborator 场景。因此不能仅因能写自己的 Fork，就宣称可以完成任意第三方上游 PR 路径；[官方限制](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens-limitations)。
- Classic PAT 是上述用户身份公共贡献的候选，`public_repo` 与 `repo` 的范围不同，仍受用户本身权限约束；[PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)、[scope](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps#available-scopes)。
- GitHub App 的安装 token 不能扩展到未授权的仓库；user token 也受用户／App 权限和安装范围共同约束。读取公共资源的能力不能推导任意公共写权限；[安装 token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)、[用户 token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app-on-behalf-of-a-user)。
- App 创建 Fork 有额外安装条件，不能假定用户可以在第三方上游安装 App；[Create a fork](https://docs.github.com/en/rest/repos/forks#create-a-fork)、[安装要求](https://docs.github.com/en/apps/using-github-apps/installing-a-github-app-from-a-third-party#requirements-to-install-a-github-app)。

## 轮询与同步

- 本地服务可使用固定频率的认证条件请求，保存各响应的 ETag／Last-Modified，遵守限额、退避与适用的轮询间隔。不能保证每个端点都有相同 validator；[最佳实践](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)。
- 分页须跟随 Link，第一页没有变化不能证明其他页没有变化；列表顺序可能随更新改变，客户端需要对象 ID 去重和补偿性复核；[分页](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)。
- Issues API 有更新时间 `since` 过滤，结果也可能包含 PR；PR 列表的过滤能力不同，不能把两者当成同一事件流；[Issues](https://docs.github.com/en/rest/issues/issues#list-repository-issues)、[PR 列表](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests)。

## PR 创建前与创建后的检查

- `pull_request` 专属工作流由 PR 事件触发，并可能检出合并 ref；PR 不存在时不能取得该 PR 本次专属检查结果；[触发与 ref](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request)。
- push CI、显式 dispatch、本地测试或固定 base／head 的合并预演可构成提前验证证据，但其权限、事件、checkout 和运行环境可能不同，不能未经核对就声称等价于上游 PR CI；[push](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#push)、[workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)、[repository dispatch](https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event)。
- Fork PR 的工作流可能需要维护者批准，未查询到检查结果不能当作通过；[Fork 限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull-request-events-for-forked-repositories)。
- CI 跟进需要关联实际提交与检查来源，综合 Actions、check runs、commit statuses 和 reviews；[Actions](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository)、[checks](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)、[statuses](https://docs.github.com/en/rest/commits/statuses#get-the-combined-status-for-a-specific-reference)、[reviews](https://docs.github.com/en/rest/pulls/reviews#list-reviews-for-a-pull-request)。

## 创建 PR 的对账

Create PR 契约未声明通用幂等键或恰好一次保证。应用应保存操作意图和预期 repo／base／head／SHA，创建前后核对远端，回执丢失先查询所有状态的 PR 并验证仓库 ID、分支和 SHA，不能只凭标题认领。

创建接口接收分支名，不提供创建时的 `expected_head_sha` 参数；前后核对仍需处理分支移动，不能声称消除了跨系统竞争窗口。

来源：[创建 PR](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request)、[列举 PR](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests)、[查询 PR](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request)、[查询 ref](https://docs.github.com/en/rest/git/refs#get-a-reference)。
