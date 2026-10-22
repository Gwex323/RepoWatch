import { test, expect } from '@playwright/test';

test('automatic repair: opt in, paused queue, progress, retry, evidence and owner PR links', async ({ page }) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const settings = { enabled: false, model_enabled: true, version: 1, max_attempts: 3 };
  const task = {
    id: 'repair_fixture', finding_id: 'finding_fixture', version: 1, revision: 'a'.repeat(40),
    status: 'queued', stage: null as string | null, attempt: 0, enabled: false,
    can_retry: false, error: null as string | null,
    timeout_recovery_available: false,
    source_read_recovery_available: false,
    failure_recovery_available: false,
    output_recovery_available: false,
    protocol_recovery_available: false,
    workspace_recovery_available: false,
    review_evidence_recovery_available: false,
    verification_resume_available: false,
    publication_retry_available: false,
    docker_recovery_available: false,
    context: { finding: { title: '上限边界处理错误' } },
    patch: { summary: '修正包含上限的边界判断，并增加边界回归测试。',
      edits: [{ path: 'bounds.py' }, { path: 'tests/test_boundary.py' }] },
    diff: '--- a/bounds.py\n+++ b/bounds.py\n-    return min(value, maximum - 1)\n+    return min(value, maximum)\n',
    verification: {
      regression_before: { exit_code: 1, log: 'AssertionError: 4 != 5', command: 'python -m unittest tests.test_boundary' },
      regression_after: { exit_code: 0, log: 'Ran 1 test\nOK', command: 'python -m unittest tests.test_boundary' },
      full_suite_after: { exit_code: 0, log: 'Ran 2 tests\nOK', command: 'python -m unittest discover' },
      passed: true,
    },
    review: { approved: true, summary: '修改针对已确认的问题，新增测试验证了上限条件。', risks: ['仅验证当前源码版本。'] },
    remote: null as null | { branch: string; pr: { number: number; url: string }; issue: { number: number; url: string } },
    events: [{ kind: 'handoff', value: 'repair_agent -> independent_reviewer', created: 1 }],
    attempts: [] as { attempt: number; error: string; diff: string; review: null }[],
  };
  const env = { id: 'env_repair', version: 1, revision: task.revision, state: 'ready',
    installed: true, busy: false, created: 1, url: null, error: null, expires: null, jobs: [],
    test_result: { exit_code: 0, revision: task.revision },
    config: { language: 'python', install: 'true', test: 'python -m unittest',
      start: '', port: 8000, install_network: false, detected: true } };
  await page.route('**/api/system', async route => {
    const response = await route.fetch();
    await route.fulfill({ json: { ...await response.json(), automatic_repair: settings } });
  });
  await page.route('**/api/repair-settings', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      expect(body.version).toBe(settings.version);
      settings.enabled = body.enabled;
      settings.version++;
      task.enabled = settings.enabled;
    }
    await route.fulfill({ json: settings });
  });
  await page.route('**/api/execution/readiness', route => route.fulfill({ json: { ready: true } }));
  await page.route('**/api/**/environments', route => route.fulfill({ json: [env] }));
  await page.route('**/api/environments/env_repair', route => route.fulfill({ json: env }));
  await page.route('**/api/environments/env_repair/analyses', route => route.fulfill({ json: [] }));
  await page.route('**/api/environments/env_repair/repairs', route => route.fulfill({ json: [task] }));
  await page.route('**/api/repairs/repair_fixture/retry', async route => {
    expect(route.request().postDataJSON().version).toBe(task.version);
    task.status = 'queued'; task.error = null; task.can_retry = false; task.version++;
    task.timeout_recovery_available = false;
    task.source_read_recovery_available = false;
    task.failure_recovery_available = false;
    task.output_recovery_available = false;
    task.protocol_recovery_available = false;
    task.workspace_recovery_available = false;
    task.review_evidence_recovery_available = false;
    task.verification_resume_available = false;
    task.publication_retry_available = false;
    task.docker_recovery_available = false;
    task.attempts = [{ attempt: 1, error: 'repair_tests_failed', diff: task.diff, review: null }];
    await route.fulfill({ json: task });
  });
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  if (await page.getByRole('heading', { name: '创建本地工作空间' }).isVisible()) {
    await page.getByLabel('确认密码', { exact: true }).fill('qa-only-owner-password');
    await page.getByRole('button', { name: '创建并进入' }).click();
  } else await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByLabel('GitHub 仓库链接').fill('https://github.com/fixture/repair');
  await page.getByRole('dialog').getByRole('button', { name: '添加仓库', exact: true }).click();
  const open = async () => page.locator('.repository-row').filter({ hasText: 'fixture/repair' })
    .getByRole('button', { name: '管理运行环境' }).click();
  await open();
  const panel = page.getByLabel('自动修复任务');
  await expect(panel.getByText('等待启用自动修复', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '运行配置', exact: true }).click();
  const config = page.getByLabel('自动修复配置');
  await expect(config.getByRole('checkbox')).not.toBeChecked();
  await config.getByRole('checkbox').check();
  await config.getByRole('button', { name: '保存修复配置' }).click();
  await expect(config.getByRole('status')).toHaveText('自动修复配置已保存');
  await expect(page.getByText('Issue / PR 发布：已启用', { exact: true })).toBeVisible();
  await expect(page.getByText('已启用 · 测试与审查通过后提交', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/repair-settings-desktop.png', fullPage: true });
  await page.getByRole('button', { name: '仓库', exact: true }).click();
  await open();
  task.status = 'running'; task.stage = 'independent_reviewer'; task.attempt = 1;
  task.events.push({ kind: 'request_started', value: JSON.stringify({
    role: 'independent_reviewer', request: 1, timeout_seconds: 450,
  }), created: 2 });
  await expect(panel.getByText('修复执行中', { exact: true })).toBeVisible();
  await expect(panel.getByText('独立审查 · 第 1 次请求 · 等待上限 450 秒', { exact: true })).toBeVisible();
  await panel.getByText('修复验证记录', { exact: true }).click();
  await expect(panel.getByText('修复前回归测试 · exit 1')).toBeVisible();
  await panel.getByText('代码差异', { exact: true }).click();
  await page.screenshot({ path: 'test-results/repair-progress-desktop.png', fullPage: true });
  task.status = 'failed'; task.error = 'repair_tests_failed'; task.can_retry = true;
  task.events.push({ kind: 'source_blocked', value: JSON.stringify({
    path: 'backend/config.py', line: 21, phase: 'read', rule: 'credential_literal',
  }), created: 2 });
  await expect(panel.getByRole('alert')).toContainText('修复前失败');
  await expect(panel.getByText('backend/config.py:21 · 源码读取 · 疑似硬编码凭据', { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await panel.getByRole('button', { name: '重试修复' }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  task.status = 'failed'; task.error = 'repair_model_timeout'; task.can_retry = true;
  task.attempt = 3; task.timeout_recovery_available = true;
  await expect(panel.getByText('超时策略已更新，此任务可额外重试一次。', { exact: true })).toBeVisible();
  await panel.getByRole('button', { name: '使用新策略重试' }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: '使用新策略重试' })).toHaveCount(0);
  task.status = 'failed'; task.error = 'repair_file_unavailable'; task.can_retry = true;
  task.attempt = 4; task.source_read_recovery_available = true;
  task.events.push({ kind: 'source_unavailable', value: JSON.stringify({
    path: 'bounds.py', candidate: true, reason: 'candidate_not_ready',
  }), created: 3 });
  await expect(panel.getByText('文件读取策略已更新，此任务可额外重试一次。', { exact: true })).toBeVisible();
  await expect(panel.getByText('bounds.py · 修复版本 · 补丁尚未生成，应读取原版本', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/repair-read-recovery-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.getByRole('button', { name: '使用新策略重试' }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: '使用新策略重试' })).toHaveCount(0);
  task.status = 'failed'; task.error = 'repair_failed'; task.can_retry = true;
  task.attempt = 5; task.failure_recovery_available = true;
  task.events.push({ kind: 'tool_arguments_invalid', value: JSON.stringify({
    tool: 'submit_and_test_repair', fields: ['patch.edits.[].original_sha256'],
    reason: 'arguments_schema', corrections_remaining: 1,
  }), created: 4 });
  task.events.push({ kind: 'failure_details', value: JSON.stringify({
    code: 'repair_output_invalid', exceptions: ['ModelBehaviorError'],
    locations: [{ file: 'repowatch/repair_model.py', line: 1 }],
  }), created: 5 });
  await expect(panel.getByRole('alert')).toContainText('修复流程发生未分类错误');
  await expect(panel.getByText('错误处理已更新，此任务可额外重试一次。', { exact: true })).toBeVisible();
  await expect(panel.getByText('提交修复补丁 · 参数结构不匹配 · patch.edits.[].original_sha256', { exact: true })).toBeVisible();
  await panel.getByText('修复执行记录', { exact: true }).click();
  await expect(panel.getByText(/异常类型：ModelBehaviorError · 定位：repowatch\/repair_model.py:1/)).toBeVisible();
  await page.screenshot({ path: 'test-results/repair-tool-recovery-mobile.png', fullPage: true });
  await panel.getByRole('button', { name: '使用新策略重试' }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: '使用新策略重试' })).toHaveCount(0);
  task.status = 'failed'; task.error = 'repair_output_invalid'; task.can_retry = true;
  task.attempt = 6; task.output_recovery_available = true;
  task.events.push({ kind: 'provider_response', value: JSON.stringify({
    truncated: true, output_tokens: 16000, reasoning_tokens: 15922,
  }), created: 6 });
  await expect(panel.getByText('本次失败源于输出被截断。输出策略已更新，此任务可额外重试一次。', { exact: true })).toBeVisible();
  await expect(panel.getByRole('alert')).toContainText('输出达到上限');
  task.events.push({ kind: 'output_reasoning_reduced', value: JSON.stringify({
    role: 'repair_agent', from: 'high', to: 'low',
  }), created: 6 });
  await expect(panel.getByText('输出超限，已临时降低思考强度', { exact: false })).toBeVisible();
  await expect(panel.getByText(/输出已截断 · 输出 16000 token · 其中推理 15922 token/)).toBeVisible();
  await page.screenshot({ path: 'test-results/repair-output-recovery-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.getByRole('button', { name: '使用新策略重试' }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  task.status = 'failed'; task.error = 'repair_output_truncated';
  await expect(panel.getByRole('alert')).toContainText('输出达到上限');
  await expect(panel.getByRole('button', { name: '使用新策略重试' })).toHaveCount(0);
  task.status = 'failed'; task.error = 'repair_tests_failed'; task.can_retry = true;
  task.attempt = 7; task.protocol_recovery_available = true;
  await expect(panel.getByRole('alert')).toContainText('补丁尚未完成真实测试验证');
  await expect(panel.getByText('补丁参数未通过校验，尚未执行回归测试。参数兼容与交接策略已更新，可额外重试一次。', { exact: true })).toBeVisible();
  task.events.push({ kind: 'tool_arguments_normalized', value: JSON.stringify({
    tool: 'submit_and_test_repair', shape: 'encoded_patch',
  }), created: 7 });
  await expect(panel.getByText(/提交修复补丁 · 已兼容编码为字符串的补丁参数，仍执行完整校验/)).toBeVisible();
  await page.screenshot({ path: 'test-results/repair-protocol-recovery-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.getByRole('button', { name: '使用新策略重试' }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: '使用新策略重试' })).toHaveCount(0);
  task.status = 'failed'; task.error = 'repair_handoff_incomplete'; task.can_retry = true;
  task.review_evidence_recovery_available = true;
  task.events.push({ kind: 'review_evidence_retry', value: 'independent_reviewer', created: 8 });
  await expect(panel.getByRole('alert')).toContainText('独立审查未读取验证证据');
  await expect(panel.getByText('补丁已通过测试，但旧版审查遗漏证据读取。可额外恢复一次：重新验证已保存补丁后继续独立审查。', { exact: true })).toBeVisible();
  await expect(panel.getByText(/审查提前返回结论，已要求先读取验证证据/)).toBeVisible();
  await panel.getByRole('button', { name: '使用新策略重试' }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  task.status = 'failed'; task.error = 'docker_unavailable'; task.can_retry = true;
  task.attempt = 8; task.workspace_recovery_available = true;
  task.verification_resume_available = true;
  await expect(panel.getByRole('alert')).toContainText('Docker 不可用');
  await expect(panel.getByText('补丁已保留，重试将继续测试验证，通过后进入独立审查。', { exact: true })).toBeVisible();
  await panel.getByRole('button', { name: '使用新策略重试' }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  task.status = 'failed'; task.error = 'repair_unsafe_path'; task.can_retry = true;
  task.workspace_recovery_available = true;
  task.events.push({ kind: 'path_rejected', value: JSON.stringify({
    reason: 'sensitive_name', edit_index: 1,
  }), created: 8 });
  await expect(panel.getByRole('alert')).toContainText('文件路径未通过保护检查');
  await expect(panel.getByText('第 2 个修改 · 文件名包含受保护的敏感词', { exact: true })).toBeVisible();
  task.error = 'repair_source_path_rejected';
  task.events.push({ kind: 'source_path_rejected', value: JSON.stringify({
    reason: 'unsupported_file_type', recoverable: false, corrections_remaining: 0,
  }), created: 9 });
  await expect(panel.getByRole('alert')).toContainText('读取的文件类型不在允许的源码范围内');
  await expect(panel.getByText('源码读取 · 文件类型不在允许读取的源码范围内', { exact: true }).first()).toBeVisible();
  await expect(panel.getByText('运行环境与路径处理已更新，此任务可额外重试一次。', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/repair-workspace-recovery-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.getByRole('button', { name: '使用新策略重试' }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  task.status = 'waiting_environment'; task.error = 'docker_unavailable';
  task.attempt = 0; task.can_retry = true;
  await expect(panel.getByText('等待 Docker 环境', { exact: true })).toBeVisible();
  await expect(panel.getByRole('status')).toContainText('本次等待不占用修复次数');
  await expect(panel.getByRole('alert')).toHaveCount(0);
  await expect(panel.getByText(/已尝试 0 次/)).toBeVisible();
  await page.screenshot({ path: 'test-results/repair-waiting-environment-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  task.status = 'running'; task.stage = 'repair_agent'; task.error = null; task.attempt = 1;
  await expect(panel.getByText('修复执行中', { exact: true })).toBeVisible();
  task.status = 'failed'; task.error = 'docker_unavailable'; task.attempt = 6;
  task.docker_recovery_available = true; task.can_retry = true;
  await expect(panel.getByRole('alert')).toContainText('上次失败：Docker 不可用');
  await expect(panel.getByRole('button', { name: '继续修复', exact: true })).toBeVisible();
  await panel.getByRole('button', { name: '继续修复', exact: true }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  task.status = 'failed'; task.stage = 'publishing'; task.error = 'github_permission_denied';
  task.can_retry = true; task.publication_retry_available = true;
  task.events.push({ kind: 'failure_details', value: JSON.stringify({
    github: { status: 403, method: 'POST', operation: 'git_blob', remaining: 4977,
      required_permissions: { contents: 'write' } },
  }), created: 9 });
  await expect(panel.getByRole('alert')).toContainText('GitHub 令牌权限不足');
  await expect(panel.getByRole('button', { name: '仅重试发布', exact: true })).toBeVisible();
  await expect(panel.getByText(/GitHub HTTP 403 · 写入文件内容 · Contents 需要写入权限 · 剩余配额 4977/)).toBeVisible();
  await page.screenshot({ path: 'test-results/repair-github-permission-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  task.events.push({ kind: 'failure_details', value: JSON.stringify({
    github: { status: 403, method: 'POST', operation: 'issue',
      permission_alternatives: [{ issues: 'write' }, { pull_requests: 'write' }] },
  }), created: 9 });
  await expect(panel.getByText(/可选权限方案：Issues 需要写入权限 或 Pull requests 需要写入权限/)).toBeVisible();
  task.error = 'github_rate_limited';
  task.events.push({ kind: 'failure_details', value: JSON.stringify({
    github: { status: 429, method: 'POST', operation: 'pull_request', retry_after: 60, remaining: 0 },
  }), created: 10 });
  await expect(panel.getByRole('alert')).toContainText('GitHub 请求已被限流');
  await expect(panel.getByText(/GitHub HTTP 429 · PR · 剩余配额 0 · 至少等待 60 秒/)).toBeVisible();
  await panel.getByRole('button', { name: '仅重试发布', exact: true }).click();
  await expect(panel.getByText('等待修复', { exact: true })).toBeVisible();
  task.error = null;
  task.status = 'pr_created'; task.stage = 'pr_created'; task.attempt = 9;
  task.remote = {
    branch: 'repowatch/repair-fixture',
    issue: { number: 11, url: 'https://github.com/fixture/repair/issues/11' },
    pr: { number: 12, url: 'https://github.com/fixture/repair/pull/12' },
  };
  await expect(panel.getByRole('link', { name: 'PR #12' })).toHaveAttribute('href', task.remote.pr.url);
  await expect(panel.getByRole('link', { name: 'Issue #11' })).toHaveAttribute('href', task.remote.issue.url);
  await expect(panel.getByRole('button', { name: /合并/ })).toHaveCount(0);
  await panel.getByText('历史尝试', { exact: true }).click();
  await expect(panel.getByText('第 1 次尝试')).toBeVisible();
  await page.screenshot({ path: 'test-results/repair-result-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});
