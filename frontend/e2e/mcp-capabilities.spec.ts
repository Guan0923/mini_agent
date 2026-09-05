import { expect, test } from "@playwright/test";

test("HTTP settings and all resource/prompt tools work through a real Agent turn", async ({ page }, testInfo) => {
  const port = process.env.MINI_AGENT_E2E_MCP_PORT ?? "18282";
  await page.setViewportSize({ width: 1365, height: 1000 });
  await page.goto("/");
  await page.getByRole("button", { name: /个人简介：/ }).click();
  await page.getByRole("menuitem", { name: "MCP", exact: true }).click();
  await page.getByRole("button", { name: /新增 MCP Server/ }).click();
  await page.getByLabel("MCP Server 名称").fill("browser");
  await page.getByText("Streamable HTTP", { exact: true }).click();
  await page.getByLabel("MCP URL new").fill(`http://127.0.0.1:${port}/mcp`);
  await expect(page.getByText("HTTP 会明文传输请求头和内容。")).toBeVisible();
  await page.getByRole("button", { name: "创建 Server" }).click();
  await expect(page.getByText("browser", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /browser/ }).click();
  const tested = page.waitForResponse((response) => response.url().endsWith("/servers/browser/test"));
  await page.getByRole("button", { name: "测试连接" }).click();
  const response = await tested;
  expect(response.ok()).toBeTruthy();
  expect(await response.json()).toMatchObject({ protocol_version: "2026-07-28", counts: { resources: 2, resource_templates: 1, prompts: 2, tools: 3 } });
  await page.screenshot({ path: testInfo.outputPath("mcp-settings-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath("mcp-settings-mobile.png") });
  expect(await page.locator(".user-settings-detail").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy();
  await page.setViewportSize({ width: 1365, height: 1000 });
  await page.getByRole("switch", { name: "启用 MCP", exact: true }).check();
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await page.request.post("/api/sidebar-threads", { data: { title: "Other MCP QA" } });
  const created = await page.request.post("/api/sidebar-threads", { data: { title: "MCP Feature QA" } });
  expect(created.ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByRole("button", { name: "MCP Feature QA", exact: true })).toBeEnabled({ timeout: 20000 });
  await page.getByRole("button", { name: "MCP Feature QA", exact: true }).click();
  await page.getByRole("textbox", { name: "聊天输入" }).fill("Use the MCP resource and prompt workflow.");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  for (const tool of ["list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource", "list_mcp_prompts", "get_mcp_prompt", "subscribe_mcp_resource", "get_mcp_resource_updates", "unsubscribe_mcp_resource"]) {
    const decision = page.locator(".tool-decision").filter({ has: page.getByText(tool, { exact: true }) });
    const allow = decision.getByRole("button", { name: "本次允许", exact: true });
    await expect(allow).toBeVisible({ timeout: 20000 });
    await allow.click();
  }
  await expect(page.locator(".message.assistant").last()).toContainText("MCP resource and prompt workflow completed.", { timeout: 20000 });
  const results = await (await page.request.get("/api/test/mcp-results")).json();
  expect(results.results).toHaveLength(8);
  expect(JSON.stringify(results)).toContain("resource revision 0");
  expect(JSON.stringify(results)).toContain("Review in Chinese");
  expect(results.results[6].content).toContain("active");
  await page.screenshot({ path: testInfo.outputPath("mcp-conversation.png") });
  await page.getByRole("button", { name: "Trace", exact: true }).click();
  for (const tool of ["read_mcp_resource", "get_mcp_prompt"]) {
    const header = page.getByRole("button", { name: new RegExp(`MCP ${tool}$`) });
    await header.click();
    await expect(header.locator("xpath=..").locator(".trace-value")).toContainText(tool);
  }
  await page.screenshot({ path: testInfo.outputPath("mcp-trace.png") });
});
