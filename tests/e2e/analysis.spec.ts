import { test, expect } from '@playwright/test';

for (const feedback of [
  { decision: 'confirmed', note: '', button: '复现成功', status: '已确认 · 待接入修复' },
  { decision: 'not_reproduced', note: '按步骤操作后未观察到异常。', button: '未能复现', status: '未能复现' },
]) {
test(`analysis settings, automatic status, evidence and human reproduction on mobile (${feedback.decision})`, async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  let enabled = false;
  let keySaved = false;
  let retried = false;
  let formatFailure = false;
  let timeoutFailure = false;
  let evidenceFailure = false;
  const provider = { enabled: false, model: '', base_url: 'https://api.openai.com/v1',
    api: 'responses', has_key: false, version: 0, output_protocol: 'json_schema',
    finalization_mode: 'provider_default' };
  const analysis = {
    id: 'analysis_fixture', version: 1, revision: 'a'.repeat(40), status: 'waiting_model',
    current: true, created: Date.now() / 1000, error: null,
    report: { summary: '发现一个需要人工复现的疑似边界问题。', limitations: ['仅分析当前快照。'],
      files_read: ['app.py'], model: 'test-only' },
    coverage: { captured: 1, eligible: 1, complete: true },
    events: [{ kind: 'handoff', agent: 'repository_analyst -> problem_investigator', created: 1 }],
    findings: [{ id: 'finding_fixture', version: 1, status: 'waiting_reproduction', data: {
      title: '边界条件疑似处理错误', severity: 'P2', explanation: '这是浏览器测试中的模拟发现。',
      expected: '返回空列表', suspected_actual: '返回上一页', reproduction: ['输入超出范围的页码。'],
      evidence: [{ path: 'app.py', start_line: 1, end_line: 1, quote: 'return previous_page' }],
    } }],
  };
  const env = { id: 'env_analysis', version: 1, revision: 'a'.repeat(40), state: 'ready',
    installed: true, busy: false, created: 1, url: null, error: null, expires: null, jobs: [],
    test_result: { exit_code: 0, revision: 'a'.repeat(40) },
    config: { language: 'python', install: '', test: 'python -m pytest', start: '', port: 8000,
      install_network: false, detected: true } };
  await page.route('**/api/analysis-model', async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      expect(body.api_key).toBe('test-key-not-a-real-credential');
      expect(body.reasoning_effort).toBe('auto');
      keySaved = true; enabled = true;
      Object.assign(provider, { ...body, has_key: true, version: 1,
        output_protocol: 'json_object', finalization_mode: 'glm47_direct' });
      delete (provider as Record<string, unknown>).api_key;
    }
    await route.fulfill({ json: provider });
  });
  await page.route('**/api/execution/readiness', route => route.fulfill({ json: { ready: true } }));
  await page.route('**/api/**/environments', route => route.fulfill({ json: [env] }));
  await page.route('**/api/environments/env_analysis', route => route.fulfill({ json: env }));
  await page.route('**/api/environments/env_analysis/analyses', route => route.fulfill({
    json: [{ ...analysis, status: enabled ? (retried ? 'completed' : 'failed') : 'waiting_model',
      error: enabled && !retried ? (evidenceFailure ? 'analysis_evidence_unverified'
        : timeoutFailure ? 'analysis_provider_timeout_exhausted'
        : formatFailure ? 'analysis_json_invalid' : 'analysis_output_truncated') : null,
      failure_details: enabled && !retried ? {
        stage: formatFailure ? 'independent_reviewer' : 'reproduction_assistant',
        elapsed_seconds: evidenceFailure ? 399 : timeoutFailure ? 573 : formatFailure ? 135 : 229, http_status: null,
      } : null,
      provider_response: enabled && !retried ? {
        finish_reason: formatFailure ? 'stop' : 'length', output_tokens: formatFailure ? 31 : 6000,
        reasoning_tokens: formatFailure ? 1 : 5999, truncated: !formatFailure,
      } : null,
      events: evidenceFailure ? [...analysis.events,
        { kind: 'evidence_normalized', agent: '1', created: 2 },
        { kind: 'evidence_validation', agent: JSON.stringify({
          finding: 1, evidence: 2, reason: 'quote_mismatch',
        }), created: 3 },
        { kind: 'evidence_retry', agent: 'independent_reviewer', created: 4 }] : timeoutFailure ? [...analysis.events,
        { kind: 'report_finalization', agent: 'independent_reviewer', created: 2 },
        { kind: 'finalization_thinking', agent: 'disabled', created: 3 },
        { kind: 'timeout_retry', agent: 'independent_reviewer', created: 4 },
        { kind: 'retry_call', agent: 'independent_reviewer', created: 5 }] : formatFailure ? [...analysis.events,
        { kind: 'report_validation', agent: 'invalid_json', created: 2 },
        { kind: 'report_diagnostics', agent: JSON.stringify({
          category: 'textual_tool_call', text_chars: 196, line: 1, column: 1,
        }), created: 3 },
        { kind: 'format_retry', agent: 'independent_reviewer', created: 4 },
        { kind: 'report_finalization', agent: 'independent_reviewer', created: 5 }] : analysis.events,
      progress: { stage: 'repository_analyst', model_calls: 5, source_reads: 8 },
      report: retried ? analysis.report : null, findings: retried ? analysis.findings : [] }],
  }));
  await page.route('**/api/analyses/analysis_fixture/retry', async route => {
    expect(route.request().postDataJSON().version).toBe(1);
    retried = true;
    await route.fulfill({ json: { ...analysis, status: 'queued' } });
  });
  await page.route('**/api/analyses/analysis_fixture/findings/finding_fixture/feedback', async route => {
    const body = route.request().postDataJSON();
    expect(body.decision).toBe(feedback.decision);
    expect(body.personally_reproduced).toBe(feedback.decision === 'confirmed');
    expect(body.note).toBe(feedback.note);
    analysis.findings[0].status = feedback.decision;
    await route.fulfill({ json: analysis });
  });
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  if (await page.getByRole('heading', { name: '创建本地工作空间' }).isVisible()) {
    await page.getByLabel('确认密码', { exact: true }).fill('qa-only-owner-password');
    await page.getByRole('button', { name: '创建并进入' }).click();
  } else await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByLabel('GitHub 仓库链接').fill('https://github.com/fixture/analysis');
  await page.getByRole('dialog').getByRole('button', { name: '添加仓库', exact: true }).click();
  const open = async () => page.locator('.repository-row').filter({ hasText: 'fixture/analysis' })
    .getByRole('button', { name: '管理运行环境' }).click();
  await open();
  await expect(page.getByLabel('仓库自动分析').getByText('等待模型配置', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '运行配置', exact: true }).click();
  await page.getByLabel('分析模型名称').fill('glm-4.7');
  await page.locator('label').filter({ hasText: '模型接口' }).locator('select').selectOption('chat_completions');
  await page.locator('label').filter({ hasText: '模型服务地址' }).locator('input').fill('https://open.bigmodel.cn/api/paas/v4');
  await page.getByLabel('推理强度').selectOption('auto');
  await page.getByLabel('分析模型 API Key').fill('test-key-not-a-real-credential');
  await page.getByLabel('启用仓库自动分析').check();
  await page.getByRole('button', { name: '保存分析模型' }).click();
  await expect.poll(() => keySaved).toBe(true);
  await expect(page.getByLabel('分析模型 API Key')).toHaveValue('');
  await expect(page.getByText('已保存输出协议：JSON Object')).toBeVisible();
  await expect(page.getByText('已保存报告模式：GLM-4.7 直接输出（分析阶段保持默认思考）')).toBeVisible();
  await page.screenshot({ path: 'test-results/analysis-model-desktop.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '仓库', exact: true }).click();
  await open();
  const panel = page.getByLabel('仓库自动分析');
  await expect(panel.getByText('分析未完成', { exact: true })).toBeVisible();
  await expect(panel.getByRole('alert')).toContainText('模型输出达到长度上限而被截断');
  await expect(panel.getByText('失败阶段：复现辅助 · 耗时 229 秒')).toBeVisible();
  await expect(panel.getByText('模型结束原因：length · 输出 6000 tokens · 推理 5999 tokens')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await panel.screenshot({ path: 'test-results/analysis-failure-mobile.png', animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  formatFailure = true;
  await expect(panel.getByRole('alert')).toContainText('模型返回内容未通过 JSON 校验');
  await expect(panel.getByText('失败阶段：独立审查 · 耗时 135 秒')).toBeVisible();
  await panel.getByText(/^Agent 分析记录/).click();
  await expect(panel.getByText('报告格式检查：不是合法 JSON', { exact: false })).toBeVisible();
  await expect(panel.getByText('报告诊断：工具调用被当作正文返回 · 正文 196 字符', { exact: false })).toBeVisible();
  await expect(panel.getByText('独立审查 · 使用已读取证据生成最终报告', { exact: false })).toBeVisible();
  await expect(panel.getByText('独立审查 · 报告格式不合格，正在纠正一次', { exact: false })).toBeVisible();
  await panel.screenshot({ path: 'test-results/analysis-format-failure-mobile.png', animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  timeoutFailure = true;
  await expect(panel.getByRole('alert')).toContainText('本次允许的自动重试已用完');
  await expect(panel.getByText('失败阶段：独立审查 · 耗时 573 秒')).toBeVisible();
  await expect(panel.getByText('最终报告请求：已关闭额外思考', { exact: false })).toBeVisible();
  await panel.screenshot({ path: 'test-results/analysis-timeout-mobile.png', animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  evidenceFailure = true;
  await expect(panel.getByRole('alert')).toContainText('源码证据未通过校验');
  await expect(panel.getByText('问题 1 · 证据 2：引用文本与源码不一致', { exact: false })).toBeVisible();
  await expect(panel.getByText('独立审查 · 源码证据不匹配，正在纠正一次', { exact: false })).toBeVisible();
  await expect(panel.getByText('源码引用已去除显示行号：1 处', { exact: false })).toBeVisible();
  await panel.screenshot({ path: 'test-results/analysis-evidence-mobile.png', animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.getByRole('button', { name: '重试分析', exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 1080 });
  await expect(panel.getByText('分析已完成', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: '复现成功', exact: true })).toBeEnabled();
  await expect(panel.getByLabel('复现说明（选填）')).toHaveValue('');
  await expect(panel.getByRole('checkbox')).toHaveCount(0);
  await panel.screenshot({ path: 'test-results/analysis-desktop.png', animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await panel.screenshot({ path: 'test-results/analysis-mobile.png', animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  if (feedback.note) await panel.getByLabel('复现说明（选填）').fill(feedback.note);
  await panel.getByRole('button', { name: feedback.button, exact: true }).click();
  await expect(panel.locator('.analysis-finding .tag')).toHaveText(feedback.status);
  await expect(panel.getByLabel('复现说明（选填）')).toHaveCount(0);
  expect(errors).toEqual([]);
});
}
