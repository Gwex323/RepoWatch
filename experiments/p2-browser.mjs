import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createServer } from "node:net";

const require = createRequire(resolve("frontend/package.json"));
const { chromium, expect } = require("@playwright/test");
const root = resolve(".cache/p2-browser", String(Date.now()));
await mkdir(root, { recursive: true });
const port = await new Promise((done, fail) => {
  const server = createServer(); server.once("error", fail);
  server.listen(0, "127.0.0.1", () => {
    const value = server.address().port; server.close(() => done(value));
  });
});
const host = spawn(resolve("backend/.venv/Scripts/python.exe"), [
  "-X", "utf8", "experiments/p2_browser_host.py", "--data-dir", resolve(root, "data"),
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
  await page.getByRole("button", { name: "仓库", exact: true }).click();
  await page.getByText("扫描清单、覆盖与本地候选", { exact: true }).click();
  await page.getByRole("button", { name: "读取／刷新扫描" }).click();
  await expect(page.getByText("浏览器候选", { exact: false }).first()).toBeVisible();
  const selectedCandidate = page.locator("article article").filter({ hasText: "浏览器候选" }).last();
  const excludedCandidate = page.locator("article article").filter({ hasText: "待排除候选" }).last();
  await selectedCandidate.getByRole("button", { name: "选择处理", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("已保存 Issue 查重／创建请求");
  await excludedCandidate.getByRole("button", { name: "排除候选", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("已持久排除此候选");
  const decidedCandidates = await page.locator("article article").allTextContents();
  expect(decidedCandidates.some(text => text.includes("浏览器候选 · selected"))).toBe(true);
  expect(decidedCandidates.some(text => text.includes("待排除候选 · excluded"))).toBe(true);
  await page.getByRole("button", { name: "打开扫描任务", exact: false }).click();
  await expect(page.getByRole("heading", { name: "任务详情" })).toBeVisible();
  await page.getByLabel("补充信息", { exact: true }).fill("补齐离线依赖并保持既有锁文件");
  await page.getByRole("button", { name: "保存补充材料", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("操作已保存");
  let state = await page.evaluate(async () => (await fetch("/api/v1/state")).json());
  expect(state.tasks.find(task => task.id === setup.task_id).mode).toBe("scan");
  expect(state.control.paused).toBe(1);
  await page.getByRole("button", { name: "取消任务", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("操作已保存");
  state = await page.evaluate(async () => (await fetch("/api/v1/state")).json());
  expect(state.tasks.find(task => task.id === setup.task_id).state).toBe("cancelled");
  expect(failures).toEqual([]);
  await page.screenshot({ path: resolve(root, "scans.png"), fullPage: true });
  const report = { status: "passed", checks: ["valid_coverage", "candidate_request", "candidate_exclusion", "task_navigation", "supplement_preserves_scan_mode", "paused_control", "cancel"], screenshot: resolve(root, "scans.png") };
  await writeFile(resolve(root, "report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  if (browser) await browser.close();
  host.kill();
}
