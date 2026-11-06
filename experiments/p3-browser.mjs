import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createServer } from "node:net";

const require = createRequire(resolve("frontend/package.json"));
const { chromium, expect } = require("@playwright/test");
const root = resolve(".cache/p3-browser", String(Date.now()));
await mkdir(root, { recursive: true });
const port = await new Promise((done, fail) => {
  const server = createServer();
  server.once("error", fail);
  server.listen(0, "127.0.0.1", () => {
    const value = server.address().port;
    server.close(() => done(value));
  });
});
const host = spawn(resolve("backend/.venv/Scripts/python.exe"), [
  "-X", "utf8", "experiments/p3_browser_host.py", "--data-dir", resolve(root, "data"),
  "--port", String(port),
], { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
let browser;
let errors = "";
host.stderr.on("data", chunk => { errors = (errors + chunk.toString()).slice(-3000); });
try {
  const setup = await new Promise((done, fail) => {
    let output = "";
    const timeout = setTimeout(() => fail(new Error("fixture timeout: " + errors)), 30000);
    host.once("exit", () => { clearTimeout(timeout); fail(new Error("fixture exited: " + errors)); });
    host.stdout.on("data", chunk => {
      output += chunk.toString();
      if (output.includes("\n")) { clearTimeout(timeout); done(JSON.parse(output.split("\n")[0])); }
    });
  });
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const failures = [];
  page.on("pageerror", error => failures.push(error.message));
  await page.goto(`http://127.0.0.1:${port}/#session=${setup.bootstrap}`);
  await expect(page.getByRole("heading", { name: "任务队列" })).toBeVisible();
  const acceptanceIndex = await page.evaluate(async id => {
    const value = await (await fetch("/api/v1/state")).json();
    return value.tasks.findIndex(task => task.id === id);
  }, setup.task_id);
  await page.locator("button.task-row").nth(acceptanceIndex).click();
  await page.getByLabel("任务类型", { exact: true }).selectOption("feature");
  await page.getByLabel("验收条件（JSON）", { exact: true }).fill(JSON.stringify({
    objective: "展示任务状态", plan: "增加只读状态视图", criteria: ["可读取当前任务状态"],
  }));
  await page.getByRole("button", { name: "保存新验收修订", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("验收规范已更新");
  await page.getByRole("button", { name: "明确批准当前验收规范", exact: true }).click();
  await expect(page.getByText("feature · 已由 local-user 批准", { exact: true })).toBeVisible();
  await page.reload();
  await page.locator("button.task-row").nth(acceptanceIndex).click();
  await expect(page.getByText("feature · 已由 local-user 批准", { exact: true })).toBeVisible();
  await page.getByLabel("补充信息", { exact: true }).fill("新增筛选范围，需要重新核验方案");
  await page.getByRole("button", { name: "保存补充材料", exact: true }).click();
  await expect(page.getByText("feature · 输入或策略已变化，需保存新修订", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "明确批准当前验收规范", exact: true })).toHaveCount(0);
  const state = await page.evaluate(async () => (await fetch("/api/v1/state")).json());
  expect(state.control.paused).toBe(1);
  const detail = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.task_id);
  expect(detail.runs).toEqual([]);
  expect(detail.acceptance.approved).toBe(false);
  await page.locator("button.task-row").filter({hasText: "information"}).click();
  await page.getByLabel("补充信息", { exact: true }).fill("完整查重结果为空，确认无相似项");
  await page.getByRole("button", { name: "确认完整查重无相似 Issue", exact: true }).click();
  await expect.poll(async () => {
    const value = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.candidate_id);
    return value.candidate_issue_decisions.length;
  }).toBe(1);
  await page.getByText("已保存的候选查重决定", {exact: true}).click();
  const candidateDetail = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.candidate_id);
  expect(candidateDetail.candidate_issue_decisions).toHaveLength(1);
  expect(candidateDetail.candidate_issue_decisions[0].decision).toBe("none");
  expect(candidateDetail.runs).toEqual([]);
  await page.locator("button.task-row").filter({hasText: "fixture_relations"}).click();
  await page.getByRole("button", {name: /^读取 Runner 日志/}).click();
  await expect(page.getByText("Runner 日志正文与摘要", {exact: true})).toBeVisible();
  await page.getByLabel("补充信息", { exact: true }).fill("已核对 PR 描述和文件差异，确认无关");
  await page.getByRole("button", {name: "确认无关", exact: true}).click();
  await expect.poll(async () => {
    const value = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.relation_task_id);
    return value.relation_decisions.length;
  }).toBe(1);
  await page.getByRole("button", {name: "请求核对远端回执", exact: true}).click();
  await expect.poll(async () => {
    const value = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.relation_task_id);
    return value.mode;
  }).toBe("reconcile_publication");
  await page.reload();
  const finalState = await page.evaluate(async () => (await fetch("/api/v1/state")).json());
  expect(finalState.control.paused).toBe(1);
  const relationDetail = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.relation_task_id);
  expect(relationDetail.relation_decisions[0].decision).toBe("unrelated");
  expect(relationDetail.publication_intents[0].state).toBe("unknown");
  expect(relationDetail.runs).toHaveLength(1);
  expect(failures).toEqual([]);
  await page.screenshot({ path: resolve(root, "acceptance.png"), fullPage: true });
  const report = {
    status: "passed", checks: ["save_spec", "approve_spec", "persist_after_reload", "stale_after_input", "paused_operations", "no_implicit_run", "candidate_lookup_decision_persisted", "pr_relation_decision_persisted", "publication_reconcile_queued_without_write", "scoped_runner_log_read"],
    screenshot: resolve(root, "acceptance.png"),
  };
  await writeFile(resolve(root, "report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  if (browser) await browser.close();
  host.kill();
}
