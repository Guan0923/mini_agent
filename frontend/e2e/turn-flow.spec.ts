import { expect, test } from "@playwright/test";

interface RuntimeRootResponse {
  session_id: string;
  thread_id: string;
  id: string;
}

interface RuntimeTurnResponse extends RuntimeRootResponse {
  parent_id: string;
  running_mode: "agent" | "plan";
  status: string;
  timestamp: string;
  current_data_idx: number;
  data: Array<Array<{
    role: string;
    delivery_id?: string;
    content: Array<Record<string, unknown> & { type: string; text?: string }>;
  }>>;
  agent_report_statuses?: Record<string, "success" | "failed">;
}

function isRuntimeTurnResponse(node: RuntimeRootResponse | RuntimeTurnResponse): node is RuntimeTurnResponse {
  return "data" in node;
}

async function fetchRuntimeNodes(
  page: import("@playwright/test").Page,
  sessionId: string,
): Promise<Array<RuntimeRootResponse | RuntimeTurnResponse>> {
  const response = await page.request.get(`/api/turns?session_id=${encodeURIComponent(sessionId)}`);
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  return response.json() as Promise<Array<RuntimeRootResponse | RuntimeTurnResponse>>;
}

async function send(page: import("@playwright/test").Page, text: string): Promise<void> {
  const editor = page.getByLabel("聊天输入");
  await editor.fill(text);
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().includes("/api/turns"),
  );
  await page.getByRole("button", { name: "发送" }).click();
  const response = await responsePromise;
  expect(response.ok(), `${response.status()} ${response.url()}`).toBeTruthy();
  await expect(page.locator(".message.user").last()).toContainText(text, { timeout: 15_000 });
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeVisible({ timeout: 15_000 });
}

async function distanceToBottom(locator: import("@playwright/test").Locator): Promise<number> {
  return locator.evaluate((element) => {
    const scrollContainer = element as HTMLElement;
    return scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight;
  });
}

function tracePanel(page: import("@playwright/test").Page, label: string, index = 0) {
  return page.getByText(label, { exact: true }).nth(index).locator(
    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-collapse-item ')][1]",
  );
}

test("Trace audit lists two real Turns oldest first and loads them independently", async ({ page }) => {
  const resetResponse = await page.request.post("/api/test/trace-model-reset");
  expect(resetResponse.ok(), `${resetResponse.status()} ${await resetResponse.text()}`).toBeTruthy();
  const sidebarResponse = await page.request.post("/api/sidebar-threads", { data: { title: "Trace Audit E2E" } });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();

  await page.goto("/app");
  await page.getByRole("button", { name: "Trace Audit E2E", exact: true }).click();
  const traceTask = "$trace-audit trace audit e2e";
  await page.getByLabel("聊天输入").fill(traceTask);
  const createTraceResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await createTraceResponse).ok()).toBeTruthy();
  await expect(page.locator(".message.user").last()).toContainText(traceTask, { timeout: 15_000 });
  await page.locator(".message.assistant").last().getByRole("button", { name: "本次允许" }).click();
  await expect(page.locator(".message.assistant").last()).toContainText(
    "Trace response from HTTP.", { timeout: 15_000 },
  );

  const secondTraceTask = "$trace-audit trace audit e2e second turn";
  await page.getByLabel("聊天输入").fill(secondTraceTask);
  const createSecondTraceResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await createSecondTraceResponse).ok()).toBeTruthy();
  await expect(page.locator(".message.user").last()).toContainText(secondTraceTask, { timeout: 15_000 });
  await expect(page.locator(".message.assistant").last()).toContainText(
    "Trace response from HTTP.", { timeout: 15_000 },
  );

  const nodes = (await fetchRuntimeNodes(page, (await sidebarResponse.json() as { session_id: string }).session_id))
    .filter(isRuntimeTurnResponse)
    .sort((left, right) => left.timestamp.localeCompare(right.timestamp) || left.id.localeCompare(right.id));
  expect(nodes).toHaveLength(2);

  await page.setViewportSize({ width: 480, height: 800 });
  await page.getByRole("button", { name: "Trace", exact: true }).click();
  await expect(page.getByLabel("聊天输入")).toHaveCount(0);
  await expect(page.getByText("System", { exact: true })).toHaveCount(1);
  await expect(page.getByText("MCP", { exact: true })).toHaveCount(1);
  await expect(page.getByText("User Message", { exact: true })).toHaveCount(1);

  const traceCollapse = page.locator(".trace-turn-collapse");
  const turnPanels = traceCollapse.locator(":scope > .ant-collapse-item");
  await expect(turnPanels).toHaveCount(2);
  await expect(turnPanels.nth(0)).not.toHaveClass(/ant-collapse-item-active/);
  await expect(turnPanels.nth(1)).toHaveClass(/ant-collapse-item-active/);
  const renderedTurnIds = await traceCollapse.locator(".trace-turn-id").allTextContents();
  expect(renderedTurnIds).toEqual(nodes.map((node) => node.id));

  const outerHeader = turnPanels.nth(1).locator(":scope > .ant-collapse-header");
  const outerTitle = outerHeader.locator(".trace-collapse-title");
  const system = tracePanel(page, "System");
  const innerHeader = system.locator(".ant-collapse-header");
  const innerTitle = innerHeader.locator(".trace-collapse-title");
  const systemPreview = system.locator(".trace-preview");
  await expect(traceCollapse).toBeVisible();
  await expect(systemPreview).toHaveCSS("overflow", "hidden");
  await expect(systemPreview).toHaveCSS("text-overflow", "ellipsis");
  await expect(systemPreview).toHaveCSS("white-space", "nowrap");
  const fullSystemPreview = await systemPreview.getAttribute("title");
  expect(fullSystemPreview?.length).toBeGreaterThan(100);

  for (const [header, title] of [[outerHeader, outerTitle], [innerHeader, innerTitle]] as const) {
    await expect.poll(async () => {
      const [headerBox, titleBox] = await Promise.all([header.boundingBox(), title.boundingBox()]);
      if (!headerBox || !titleBox) return false;
      return titleBox.x >= headerBox.x && titleBox.x + titleBox.width <= headerBox.x + headerBox.width + 1;
    }).toBe(true);
  }
  await expect.poll(() => traceCollapse.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);

  await turnPanels.nth(0).locator(":scope > .ant-collapse-header").click();
  await expect(turnPanels.nth(0)).toHaveClass(/ant-collapse-item-active/);
  await expect(turnPanels.nth(1)).toHaveClass(/ant-collapse-item-active/);
  await expect(page.getByText("System", { exact: true })).toHaveCount(2);
  await expect(page.getByText("MCP", { exact: true })).toHaveCount(2);
  await expect(page.getByText("User Message", { exact: true })).toHaveCount(2);
  await expect.poll(() => traceCollapse.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);

  const mcp = tracePanel(page, "MCP");
  await mcp.locator(".ant-collapse-header").click();
  await expect(mcp.locator(".trace-value")).toContainText('"server": "trace"');
  await expect(mcp.locator(".trace-value")).toContainText('"tool": "inspect_trace"');

  await system.locator(".ant-collapse-header").click();
  await expect(system.locator(".trace-value")).toContainText("User Agent Preferences");
  await expect(system.locator(".trace-value")).toContainText("Trace E2E preference: concise local audit.");
  await expect(system.locator(".trace-value")).toContainText(fullSystemPreview!);

  const reasoning = page.getByText("Assistant Reasoning", { exact: true }).last().locator(
    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-collapse-item ')][1]",
  );
  await reasoning.locator(".ant-collapse-header").click();
  await expect(reasoning.locator(".trace-value")).toContainText("Trace reasoning from HTTP.");

  const response = page.getByText("Assistant Response", { exact: true }).last().locator(
    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-collapse-item ')][1]",
  );
  await response.locator(".ant-collapse-header").click();
  await expect(response.locator(".trace-value")).toContainText("Trace response from HTTP.");

  const modelCalls = await page.request.get("/api/test/trace-model-calls");
  expect(modelCalls.ok()).toBeTruthy();
  expect((await modelCalls.json() as { calls: number }).calls).toBeGreaterThanOrEqual(3);

  const traceResponse = await page.request.get(
    `/api/turns/${encodeURIComponent(nodes[0].id)}/trace?data_idx=${nodes[0].current_data_idx}`,
  );
  expect(traceResponse.ok(), `${traceResponse.status()} ${await traceResponse.text()}`).toBeTruthy();
  const trace = await traceResponse.json() as {
    context: { system_message: string; tools: unknown[] };
    items: Array<{ item: { type: string } }>;
  };
  expect(trace.context.tools).toHaveLength(1);
  expect(trace.items.filter((entry) => entry.item.type === "text")).toHaveLength(2);
  expect(trace.items.filter((entry) => entry.item.type === "reasoning")).toHaveLength(1);
  expect(trace.items.filter((entry) => entry.item.type === "tool_call")).toHaveLength(1);
  expect(trace.items.filter((entry) => entry.item.type === "tool_result")).toHaveLength(1);
});

test("incomplete chunked model response exposes the raw network error in Chat and Trace", async ({ page }) => {
  const rawError = "Response ended prematurely";
  const removedPrefixes = ["Model stream failed", "Plan creation failed", "Decision failed"];
  const sidebarResponse = await page.request.post("/api/sidebar-threads", {
    data: { title: "Raw Chunked Error" },
  });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { session_id: string };

  await page.goto("/app");
  await page.getByRole("button", { name: "Raw Chunked Error", exact: true }).click();
  await page.getByLabel("聊天输入").fill("raw chunked error e2e");
  const createResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await createResponse).ok()).toBeTruthy();

  const assistant = page.locator(".message.assistant").last();
  await expect(assistant).toContainText(rawError, { timeout: 15_000 });
  for (const prefix of removedPrefixes) await expect(assistant).not.toContainText(prefix);
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeVisible();

  const turns = (await fetchRuntimeNodes(page, sidebar.session_id)).filter(isRuntimeTurnResponse);
  expect(turns).toHaveLength(1);
  expect(turns[0].status).toBe("failed");
  const errorItems = turns[0].data[turns[0].current_data_idx][1].content.filter((item) => item.type === "error");
  expect(errorItems).toEqual([expect.objectContaining({ message: rawError })]);

  await page.getByRole("button", { name: "Trace", exact: true }).click();
  const errorPreview = page.locator(".trace-preview").filter({ hasText: rawError }).first();
  await expect(errorPreview).toHaveText(rawError);
  const errorPanel = errorPreview.locator(
    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-collapse-item ')][1]",
  );
  await errorPanel.locator(":scope > .ant-collapse-header").click();
  await expect(errorPanel.locator(".trace-value")).toHaveText(rawError);
  for (const prefix of removedPrefixes) await expect(errorPanel).not.toContainText(prefix);
});

test("a real provider retry is visible live and remains ordered in Turn and Trace", async ({ page }) => {
  const resetResponse = await page.request.post("/api/test/trace-model-reset");
  expect(resetResponse.ok(), `${resetResponse.status()} ${await resetResponse.text()}`).toBeTruthy();
  const sidebarResponse = await page.request.post("/api/sidebar-threads", {
    data: { title: "Network Retry Visibility" },
  });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { session_id: string };

  await page.goto("/app");
  await page.getByRole("button", { name: "Network Retry Visibility", exact: true }).click();
  await page.getByLabel("聊天输入").fill("network retry visibility e2e");
  const createResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await createResponse).ok()).toBeTruthy();

  const assistant = page.locator(".message.assistant").last();
  await expect(assistant.getByRole("status", { name: "网络异常，正在重试（1/5）" })).toBeVisible({ timeout: 15_000 });
  await expect(assistant).toContainText("503 Server Error: Service Unavailable");
  await expect(assistant).toContainText("Retry recovered from local HTTP.", { timeout: 15_000 });
  await expect(assistant).toContainText("网络请求已重试（1/5）");

  const turns = (await fetchRuntimeNodes(page, sidebar.session_id)).filter(isRuntimeTurnResponse);
  expect(turns).toHaveLength(1);
  expect(turns[0].status).toBe("success");
  const items = turns[0].data[turns[0].current_data_idx][1].content;
  expect(items.map((item) => item.type)).toEqual(["retry", "text"]);
  expect(items[0]).toMatchObject({
    event: "model_retry",
    category: "network",
    attempt: 1,
    max_retries: 5,
    delay_seconds: 1,
    status: "success",
  });
  expect(String(items[0].message)).toContain("503 Server Error: Service Unavailable");
  expect(items.filter((item) => item.type === "error")).toHaveLength(0);

  const modelCalls = await page.request.get("/api/test/trace-model-calls");
  expect(modelCalls.ok()).toBeTruthy();
  expect((await modelCalls.json() as { retry_calls: number }).retry_calls).toBe(2);

  await page.getByRole("button", { name: "Trace", exact: true }).click();
  const retryTag = page.getByText("Network Retry", { exact: true });
  await expect(retryTag).toHaveCount(1);
  const retryPanel = retryTag.locator(
    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-collapse-item ')][1]",
  );
  await expect(retryPanel.locator(".trace-preview")).toContainText("503 Server Error: Service Unavailable");

  const traceResponse = await page.request.get(`/api/turns/${encodeURIComponent(turns[0].id)}/trace?data_idx=0`);
  expect(traceResponse.ok(), `${traceResponse.status()} ${await traceResponse.text()}`).toBeTruthy();
  const trace = await traceResponse.json() as { items: Array<{ item: { type: string; message?: string } }> };
  const retries = trace.items.filter((entry) => entry.item.type === "retry");
  expect(retries).toHaveLength(1);
  expect(retries[0].item.message).toContain("503 Server Error: Service Unavailable");
});

test("automatic Agent report and final answer share one Assistant reply frame", async ({ page }) => {
  test.setTimeout(60_000);
  const resetResponse = await page.request.post("/api/test/trace-model-reset");
  expect(resetResponse.ok(), `${resetResponse.status()} ${await resetResponse.text()}`).toBeTruthy();
  const sidebarResponse = await page.request.post("/api/sidebar-threads", {
    data: { title: "Grouped Agent Report" },
  });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();

  await page.goto("/app");
  await page.getByRole("button", { name: "Grouped Agent Report", exact: true }).click();
  await page.getByLabel("聊天输入").fill("agent thread navigation e2e");
  const rootTurnResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await rootTurnResponse).ok()).toBeTruthy();

  const assistantFrames = page.locator(".chat-messages > .message.assistant");
  await expect(assistantFrames).toHaveCount(1, { timeout: 15_000 });
  const frame = assistantFrames.first();
  await expect(frame).toContainText("Agent Thread tree is ready.", { timeout: 15_000 });
  await expect(frame.locator(".runtime-agent-report")).toHaveCount(1);
  await expect(frame.locator(".runtime-agent-report")).toContainText("thread_path: /root/direct");
  await expect(frame.locator(".assistant-icon")).toHaveCount(1);
});

test("Agent Thread tree streams an idle nested Agent message and keeps Chat and Trace aligned", async ({ page }) => {
  test.setTimeout(180_000);
  const resetResponse = await page.request.post("/api/test/trace-model-reset");
  expect(resetResponse.ok(), `${resetResponse.status()} ${await resetResponse.text()}`).toBeTruthy();
  const sidebarResponse = await page.request.post("/api/sidebar-threads", {
    data: { title: "Agent Thread Navigation" },
  });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { session_id: string; thread_id: string };

  await page.goto("/app");
  await page.getByRole("button", { name: "Agent Thread Navigation", exact: true }).click();
  await page.getByLabel("聊天输入").fill("agent thread navigation e2e");
  const rootTurnResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await rootTurnResponse).ok()).toBeTruthy();
  await expect(page.locator(".message.assistant").last()).toContainText("Agent Thread tree is ready.", {
    timeout: 15_000,
  });

  await page.getByRole("button", { name: "Thread", exact: true }).click();
  let tree = page.getByRole("tree", { name: "Agent Thread 树" });
  const rootItem = tree.getByText("root", { exact: true }).locator("xpath=ancestor::*[@role='treeitem'][1]");
  const rootChildrenResponse = page.waitForResponse((response) =>
    response.url().includes(`/api/agent-threads/${encodeURIComponent(sidebar.thread_id)}/children`),
  );
  await rootItem.locator(".ant-tree-switcher").click();
  const rootChildren = await rootChildrenResponse;
  expect(rootChildren.ok(), `${rootChildren.status()} ${await rootChildren.text()}`).toBeTruthy();
  const directSummary = (await rootChildren.json() as Array<{ thread_id: string }>)[0];
  expect(directSummary?.thread_id).toBeTruthy();
  const directLabel = tree.getByText("/root/direct · success", { exact: true });
  await expect(directLabel).toBeVisible();
  await expect(tree.getByText("/root/direct/nested · success", { exact: true })).toHaveCount(0);
  const directItem = directLabel.locator("xpath=ancestor::*[@role='treeitem'][1]");
  const directChildrenResponse = page.waitForResponse((response) =>
    response.url().includes(`/api/agent-threads/${encodeURIComponent(directSummary.thread_id)}/children`),
  );
  await directItem.locator(".ant-tree-switcher").click();
  const directChildren = await directChildrenResponse;
  expect(directChildren.ok(), `${directChildren.status()} ${await directChildren.text()}`).toBeTruthy();
  const nestedSummary = (await directChildren.json() as Array<{ thread_id: string }>)[0];
  expect(nestedSummary?.thread_id).toBeTruthy();
  await tree.getByText("/root/direct/nested · success", { exact: true }).click();

  const nestedThreadId = nestedSummary.thread_id;
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(nestedThreadId);
  await expect(page.locator(".message.assistant").last()).toContainText(
    "Agent Thread response from local HTTP.", { timeout: 15_000 },
  );
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "暂停", exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Thread", exact: true }).click();
  tree = page.getByRole("tree", { name: "Agent Thread 树" });
  await tree.getByText("root", { exact: true }).click();
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(sidebar.thread_id);

  let releaseStream: (() => void) | undefined;
  const streamGate = new Promise<void>((resolve) => { releaseStream = resolve; });
  const streamPattern = `**/api/agent-threads/${nestedThreadId}/stream*`;
  await page.route(streamPattern, async (route) => {
    await streamGate;
    await route.continue();
  });
  await page.getByRole("button", { name: "Thread", exact: true }).click();
  tree = page.getByRole("tree", { name: "Agent Thread 树" });
  await tree.getByText("root", { exact: true }).locator("xpath=ancestor::*[@role='treeitem'][1]")
    .locator(".ant-tree-switcher").click();
  const reloadedDirect = tree.getByText("/root/direct · success", { exact: true });
  await reloadedDirect.locator("xpath=ancestor::*[@role='treeitem'][1]").locator(".ant-tree-switcher").click();
  const blockedStreamRequest = page.waitForRequest((request) => request.url().includes(
    `/api/agent-threads/${nestedThreadId}/stream`,
  ));
  await tree.getByText("/root/direct/nested · success", { exact: true }).click();
  await blockedStreamRequest;

  const uploadResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().includes(`/api/sessions/${sidebar.session_id}/files`),
  );
  await page.locator('input[type="file"]').setInputFiles("../README.md");
  expect((await uploadResponse).ok()).toBeTruthy();
  await expect(page.getByText("README.md", { exact: true })).toBeVisible();
  const followUp = "idle nested Agent follow-up with upload";
  await page.getByLabel("聊天输入").fill(followUp);
  const messageRequest = page.waitForRequest((request) =>
    request.method() === "POST" && request.url().includes(`/api/agent-threads/${nestedThreadId}/messages`),
  );
  const messageResponse = page.waitForResponse((response) =>
    response.request().method() === "POST"
      && response.url().includes(`/api/agent-threads/${nestedThreadId}/messages`),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const postedMessage = (await messageRequest).postDataJSON() as {
    content: string;
    references: Array<{ source: string; path: string }>;
  };
  expect(postedMessage.content).toBe(followUp);
  expect(postedMessage.references).toHaveLength(1);
  expect(postedMessage.references[0]).toMatchObject({ source: "upload" });
  expect((await messageResponse).status()).toBe(202);
  await expect(page.locator(".message.user.is-pending")).toContainText(followUp);
  await expect(page.locator(".agent-message-pending")).toContainText("正在交给主 Agent 转发");
  await expect(page.locator(".composer-uploads")).toHaveCount(0);

  releaseStream?.();
  await expect(page.locator(".message.user.is-pending")).toHaveCount(0, { timeout: 15_000 });
  await page.unroute(streamPattern);
  await expect(page.locator(".message.user").filter({ hasText: followUp })).toHaveCount(1);
  await expect(page.locator(".message.assistant").last()).toContainText(
    "Agent Thread response from local HTTP.", { timeout: 15_000 },
  );

  const nestedTurns = (await fetchRuntimeNodes(page, sidebar.session_id))
    .filter(isRuntimeTurnResponse)
    .filter((node) => node.thread_id === nestedThreadId)
    .sort((left, right) => left.timestamp.localeCompare(right.timestamp) || left.id.localeCompare(right.id));
  expect(nestedTurns).toHaveLength(2);
  const directTurns = (await fetchRuntimeNodes(page, sidebar.session_id))
    .filter(isRuntimeTurnResponse)
    .filter((node) => node.thread_id === directSummary.thread_id);
  expect(directTurns).toHaveLength(1);
  const directMessages = directTurns[0].data[directTurns[0].current_data_idx];
  expect(directMessages.filter((message) => message.role === "assistant").length).toBeGreaterThanOrEqual(2);
  expect(directMessages.some((message) => message.content.some((item) =>
    item.type === "tool_result"
      && item.tool === "pause_current_turn"
      && item.content === "thread_status: paused"
  ))).toBeTruthy();
  expect(Object.values(directTurns[0].agent_report_statuses ?? {})).toEqual(["success"]);
  expect(directMessages.some((message) => message.content.some((item) =>
    item.type === "subagent"
      && item.event === "agent_report"
      && item.text?.startsWith("thread_path: /root/direct/nested\nthread_status: success\ntask_result: ")
  ))).toBeTruthy();
  const latestUser = nestedTurns[1].data[nestedTurns[1].current_data_idx]
    .find((message) => message.role === "user");
  const canonicalReferences = latestUser?.content[0].references as Array<{ path: string }>;
  expect(canonicalReferences).toHaveLength(1);
  expect(Object.keys(canonicalReferences[0])).toEqual(["path"]);
  expect(canonicalReferences[0].path).toMatch(/^(?:[A-Za-z]:[\\/]|\/)/);
  expect(canonicalReferences[0].path.replaceAll("\\", "/")).toMatch(/\/workspace\/uploads\/README\.md$/);

  await page.getByRole("button", { name: "Trace", exact: true }).click();
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(nestedThreadId);
  await expect(page.locator(".trace-turn-collapse > .ant-collapse-item")).toHaveCount(2);
  const userTrace = page.getByText("User Message", { exact: true }).last().locator(
    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-collapse-item ')][1]",
  );
  await userTrace.locator(".ant-collapse-header").click();
  await expect(userTrace.locator(".trace-value")).toContainText(followUp);
  const assistantTrace = page.getByText("Assistant Response", { exact: true }).last().locator(
    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-collapse-item ')][1]",
  );
  await assistantTrace.locator(".ant-collapse-header").click();
  await expect(assistantTrace.locator(".trace-value")).toContainText("Agent Thread response from local HTTP.");

  const modelCalls = await page.request.get("/api/test/trace-model-calls");
  expect(modelCalls.ok()).toBeTruthy();
  expect((await modelCalls.json() as { calls: number }).calls).toBeGreaterThanOrEqual(2);

  await page.getByRole("button", { name: "Thread", exact: true }).click();
  tree = page.getByRole("tree", { name: "Agent Thread 树" });
  await tree.getByText("root", { exact: true }).click();
  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(sidebar.thread_id);

  const forkResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/fork"),
  );
  await page.locator(".message.assistant").last().getByRole("button", { name: "Fork" }).click();
  const forkResponse = await forkResponsePromise;
  expect(forkResponse.ok(), `${forkResponse.status()} ${await forkResponse.text()}`).toBeTruthy();
  const forked = await forkResponse.json() as {
    sidebar_thread: { session_id: string; thread_id: string };
  };
  expect(forked.sidebar_thread.session_id).toBe(sidebar.session_id);
  expect(forked.sidebar_thread.thread_id).not.toBe(sidebar.thread_id);
  await expect(page.getByRole("button", { name: "Agent Thread Navigation（分支）", exact: true })).toBeVisible();
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(forked.sidebar_thread.thread_id);

  await page.getByRole("button", { name: "Thread", exact: true }).click();
  tree = page.getByRole("tree", { name: "Agent Thread 树" });
  const forkRootItem = tree.getByText("root", { exact: true }).locator("xpath=ancestor::*[@role='treeitem'][1]");
  const emptyForkChildrenResponse = page.waitForResponse((response) =>
    response.url().includes(
      `/api/agent-threads/${encodeURIComponent(forked.sidebar_thread.thread_id)}/children`,
    ),
  );
  await forkRootItem.locator(".ant-tree-switcher").click();
  const emptyForkChildren = await emptyForkChildrenResponse;
  expect(emptyForkChildren.ok(), `${emptyForkChildren.status()} ${await emptyForkChildren.text()}`).toBeTruthy();
  expect(await emptyForkChildren.json()).toEqual([]);
  await expect(tree.getByText("/root/direct · success", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Thread", exact: true }).click();
  await page.getByLabel("聊天输入").fill("agent thread navigation e2e");
  const forkTurnResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await forkTurnResponse).ok()).toBeTruthy();
  await expect(page.locator(".message.assistant").last()).toContainText("Agent Thread tree is ready.", {
    timeout: 15_000,
  });
  await expect(page.getByRole("button", { name: "暂停", exact: true })).toHaveCount(0, { timeout: 15_000 });

  await page.getByRole("button", { name: "Thread", exact: true }).click();
  tree = page.getByRole("tree", { name: "Agent Thread 树" });
  const reloadedForkRoot = tree.getByText("root", { exact: true }).locator(
    "xpath=ancestor::*[@role='treeitem'][1]",
  );
  const forkChildrenResponse = page.waitForResponse((response) =>
    response.url().includes(
      `/api/agent-threads/${encodeURIComponent(forked.sidebar_thread.thread_id)}/children`,
    ),
  );
  await reloadedForkRoot.locator(".ant-tree-switcher").click();
  const forkChildren = await forkChildrenResponse;
  expect(forkChildren.ok(), `${forkChildren.status()} ${await forkChildren.text()}`).toBeTruthy();
  const forkDirectSummary = (await forkChildren.json() as Array<{ thread_id: string }>)[0];
  expect(forkDirectSummary?.thread_id).toBeTruthy();
  expect(forkDirectSummary.thread_id).not.toBe(directSummary.thread_id);
  const forkDirectLabel = tree.getByText("/root/direct · success", { exact: true });
  await expect(forkDirectLabel).toBeVisible({ timeout: 15_000 });
  const forkDirectChildrenResponse = page.waitForResponse((response) =>
    response.url().includes(`/api/agent-threads/${encodeURIComponent(forkDirectSummary.thread_id)}/children`),
  );
  await forkDirectLabel.locator("xpath=ancestor::*[@role='treeitem'][1]").locator(".ant-tree-switcher").click();
  const forkDirectChildren = await forkDirectChildrenResponse;
  expect(forkDirectChildren.ok(), `${forkDirectChildren.status()} ${await forkDirectChildren.text()}`).toBeTruthy();
  const forkNestedSummary = (await forkDirectChildren.json() as Array<{ thread_id: string }>)[0];
  expect(forkNestedSummary?.thread_id).toBeTruthy();
  expect(forkNestedSummary.thread_id).not.toBe(nestedThreadId);
  await tree.getByText("/root/direct/nested · success", { exact: true }).click();
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(forkNestedSummary.thread_id);
  await expect(page.locator(".message.assistant").last()).toContainText(
    "Agent Thread response from local HTTP.", { timeout: 15_000 },
  );

  const originalChildren = await page.request.get(
    `/api/agent-threads/${encodeURIComponent(sidebar.thread_id)}/children?session_id=${encodeURIComponent(sidebar.session_id)}`,
  );
  const isolatedForkChildren = await page.request.get(
    `/api/agent-threads/${encodeURIComponent(forked.sidebar_thread.thread_id)}/children?session_id=${encodeURIComponent(sidebar.session_id)}`,
  );
  expect(originalChildren.ok()).toBeTruthy();
  expect(isolatedForkChildren.ok()).toBeTruthy();
  expect((await originalChildren.json() as Array<{ thread_id: string }>).map((node) => node.thread_id))
    .toEqual([directSummary.thread_id]);
  expect((await isolatedForkChildren.json() as Array<{ thread_id: string }>).map((node) => node.thread_id))
    .toEqual([forkDirectSummary.thread_id]);

  await page.getByRole("button", { name: "Agent Thread Navigation", exact: true }).click();
  await page.getByRole("button", { name: "Thread", exact: true }).click();
  tree = page.getByRole("tree", { name: "Agent Thread 树" });
  await tree.getByText("root", { exact: true }).locator("xpath=ancestor::*[@role='treeitem'][1]")
    .locator(".ant-tree-switcher").click();
  await tree.getByText("/root/direct · success", { exact: true }).locator("xpath=ancestor::*[@role='treeitem'][1]")
    .locator(".ant-tree-switcher").click();
  await tree.getByText("/root/direct/nested · success", { exact: true }).click();
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(nestedThreadId);

  await page.getByRole("button", { name: "Agent Thread Navigation（分支）", exact: true }).click();
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(forkNestedSummary.thread_id);
  await page.getByRole("button", { name: "Trace", exact: true }).click();
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(forkNestedSummary.thread_id);
  await expect(page.locator(".trace-turn-collapse > .ant-collapse-item")).toHaveCount(1);
});

test("chat stays bottom-anchored and exposes a centered translucent return button only while reading above", async ({ page }) => {
  const sidebar = await page.request.post("/api/sidebar-threads", { data: { title: "Playwright Scroll Anchor" } });
  expect(sidebar.ok(), `${sidebar.status()} ${await sidebar.text()}`).toBeTruthy();

  await page.goto("/app");
  await page.getByRole("button", { name: "Playwright Scroll Anchor", exact: true }).click();
  await expect(page.getByLabel("聊天输入")).toBeVisible();

  for (let index = 0; index < 8; index += 1) {
    await send(page, `scroll anchor history ${index}`);
  }

  const scrollContainer = page.locator("[data-conversation-scroll]");
  const returnButton = page.getByRole("button", { name: "滚动到底部" });
  await expect.poll(() => distanceToBottom(scrollContainer)).toBeLessThanOrEqual(24);
  await expect(returnButton).toHaveCount(0);

  await scrollContainer.evaluate((element) => {
    element.scrollTop = Math.max(0, element.scrollTop - 180);
  });
  await expect(returnButton).toBeVisible();

  const [buttonBox, composerBox] = await Promise.all([
    returnButton.boundingBox(),
    page.locator(".composer-box").boundingBox(),
  ]);
  expect(buttonBox).not.toBeNull();
  expect(composerBox).not.toBeNull();
  expect(Math.abs((buttonBox!.x + buttonBox!.width / 2) - (composerBox!.x + composerBox!.width / 2))).toBeLessThanOrEqual(1);
  expect(Math.abs(composerBox!.y - (buttonBox!.y + buttonBox!.height) - 12)).toBeLessThanOrEqual(1);
  await expect(returnButton).toHaveCSS("background-color", "rgba(255, 255, 255, 0.78)");
  await returnButton.hover();
  await expect(returnButton).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await page.mouse.move(0, 0);
  await returnButton.focus();
  await expect(returnButton).toHaveCSS("background-color", "rgb(255, 255, 255)");

  const readingScrollTop = await scrollContainer.evaluate((element) => element.scrollTop);
  await send(page, "sent while reading above");
  await expect.poll(async () => Math.abs((await scrollContainer.evaluate((element) => element.scrollTop)) - readingScrollTop)).toBeLessThanOrEqual(1);
  await expect(returnButton).toBeVisible();

  await returnButton.click();
  await expect.poll(() => distanceToBottom(scrollContainer)).toBeLessThanOrEqual(24);
  await expect(returnButton).toHaveCount(0);

  const editor = page.getByLabel("聊天输入");
  await editor.fill("delayed reconnect");
  const createResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await createResponse).ok()).toBeTruthy();
  await expect(page.locator(".message.assistant").last()).toContainText("Streaming began before refresh.", { timeout: 15_000 });
  await expect.poll(() => distanceToBottom(scrollContainer)).toBeLessThanOrEqual(24);
  await expect(page.locator(".message.assistant").last()).toContainText("Streaming finished after refresh.", { timeout: 15_000 });
  await expect.poll(() => distanceToBottom(scrollContainer)).toBeLessThanOrEqual(24);
  await expect(returnButton).toHaveCount(0);
});

test("first main Turn receives a dedicated model-generated title", async ({ page }) => {
  const sidebar = await page.request.post("/api/sidebar-threads", { data: {} });
  expect(sidebar.ok(), `${sidebar.status()} ${await sidebar.text()}`).toBeTruthy();

  await page.goto("/app");
  await page.getByRole("button", { name: "新对话", exact: true }).click();
  await expect(page.getByRole("navigation", { name: "主内容视图" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Thread", exact: true })).toBeVisible();
  await expect(page.getByLabel("聊天输入")).toBeVisible();
  await send(page, "请生成这个对话的模型标题");

  await expect(page.getByRole("button", { name: "浏览器生成的新标题很", exact: true })).toBeVisible();
  const nodes = await fetchRuntimeNodes(page, (await sidebar.json() as { session_id: string }).session_id);
  const firstTurn = nodes.find(isRuntimeTurnResponse);
  expect(firstTurn).toBeDefined();
  await expect(page.getByRole("navigation", { name: "主内容视图" })).toBeVisible();
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveText(firstTurn!.thread_id);
  await expect(page.locator(".trace-toolbar-thread-id")).toHaveAttribute("title", firstTurn!.thread_id);
});

test("Todo panel auto-finishes and offers cleanup only for an incomplete terminal Turn", async ({ page }) => {
  const sidebar = await page.request.post("/api/sidebar-threads", { data: { title: "Todo Lifecycle" } });
  expect(sidebar.ok(), `${sidebar.status()} ${await sidebar.text()}`).toBeTruthy();

  await page.goto("/app");
  await page.getByRole("button", { name: "Todo Lifecycle", exact: true }).click();
  const editor = page.getByLabel("聊天输入");

  await editor.fill("todo abnormal close");
  const abnormalResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await abnormalResponse).ok()).toBeTruthy();
  const todoPanel = page.locator(".todo-panel");
  await expect(todoPanel).toBeVisible({ timeout: 15_000 });
  await expect(todoPanel.locator(".ant-collapse-item")).toHaveClass(/ant-collapse-item-active/);
  await expect(page.getByRole("button", { name: "关闭任务清单", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(todoPanel.locator(".ant-collapse-item")).not.toHaveClass(/ant-collapse-item-active/);
  await expect(page.getByRole("button", { name: "关闭任务清单", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "关闭任务清单", exact: true }).click();
  await expect(todoPanel).toHaveCount(0);
  await expect(page.locator(".composer")).not.toHaveClass(/has-todo/);

  await editor.fill("todo completed auto close");
  const completedResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await completedResponse).ok()).toBeTruthy();
  await expect(page.getByText("Complete the browser lifecycle")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: "关闭任务清单", exact: true })).toHaveCount(0);
  await expect(todoPanel).toHaveCount(0, { timeout: 15_000 });
  await expect(page.locator(".composer")).not.toHaveClass(/has-todo/);
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeVisible({ timeout: 15_000 });
});

test("real Turn SSE flow supports tools, rewind versions, fork, and compact", async ({ page }) => {
  const sidebar = await page.request.post("/api/sidebar-threads", { data: { title: "Playwright Turn" } });
  expect(sidebar.ok(), `${sidebar.status()} ${await sidebar.text()}`).toBeTruthy();

  await page.goto("/app");
  await page.getByRole("button", { name: "Playwright Turn", exact: true }).click();
  await expect(page.getByLabel("聊天输入")).toBeVisible();

  await send(page, "hello");
  await expect(page.locator(".message.assistant").last()).toContainText("Hello! I can help", { timeout: 15_000 });

  await send(page, "read README.md");
  await expect(page.locator(".message.assistant").last()).toContainText("local-first Agent", { timeout: 15_000 });

  await page.locator(".message.user").first().getByRole("button", { name: "编辑" }).click();
  await page.getByRole("textbox", { name: "编辑用户消息" }).fill("rewound hello");
  const rewindResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().includes("/rewind"),
  );
  await page.getByRole("button", { name: "保存并重新生成" }).click();
  expect((await rewindResponse).ok()).toBeTruthy();
  await expect(page.locator(".message.user")).toHaveCount(1);
  await expect(page.locator(".message.user").first()).toContainText("rewound hello");
  await page.locator(".message.user").first().getByRole("button", { name: "上一个消息版本" }).click();
  await expect(page.locator(".message.user")).toHaveCount(1);
  await expect(page.locator(".message.user").first()).toContainText("hello");
  await page.locator(".message.user").first().getByRole("button", { name: "下一个消息版本" }).click();
  await expect(page.locator(".message.user")).toHaveCount(1);
  await expect(page.locator(".message.user").first()).toContainText("rewound hello");

  await send(page, "next turn");
  await expect(page.locator(".message.user").last()).toContainText("next turn");
  await page.locator(".message.user").first().getByRole("button", { name: "上一个消息版本" }).click();
  await expect(page.locator(".message.user").first()).toContainText("hello");
  await expect(page.locator(".message.user").last()).toContainText("next turn");
  await page.locator(".message.user").first().getByRole("button", { name: "下一个消息版本" }).click();
  await expect(page.locator(".message.user").first()).toContainText("rewound hello");

  await page.locator(".message.assistant").last().getByRole("button", { name: "Fork" }).click();
  await expect(page.getByRole("button", { name: "Playwright Turn", exact: true })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Playwright Turn（分支）", exact: true })).toBeVisible();
  await expect(page.locator(".message.user").last()).toContainText("next turn");

  await page.getByLabel("聊天输入").fill("/compact");
  const compactResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/compact"),
  );
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByRole("status").filter({ hasText: "正在执行compaction操作中" })).toBeVisible();
  expect((await compactResponse).ok()).toBeTruthy();
  await expect(page.getByText("正在执行compaction操作中")).toHaveCount(0);
  await expect(page.getByText("上下文已压缩", { exact: false })).toBeVisible();
});

test("Plan Review compacts and implements as Plan, Compact, Agent Turns in one SSE flow", async ({ page }) => {
  const sidebarResponse = await page.request.post("/api/sidebar-threads", {
    data: { title: "Plan Review Compact" },
  });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { session_id: string };

  await page.goto("/app");
  await page.getByRole("button", { name: "Plan Review Compact", exact: true }).click();
  await page.getByRole("combobox", { name: "运行模式" }).click();
  await page.getByRole("option", { name: /Plan/ }).click();
  await page.getByLabel("聊天输入").fill("plan review compact");
  const createResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await createResponse).ok()).toBeTruthy();

  const review = page.locator(".plan-decision");
  await expect(review).toContainText("Compact implementation plan", { timeout: 15_000 });
  const decisionResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/decisions"),
  );
  await review.getByRole("button", { name: "压缩后实施" }).click();
  expect((await decisionResponse).ok()).toBeTruthy();

  await expect(page.locator(".message.assistant").last()).toContainText(
    "Implemented the exact reviewed plan after compaction.",
    { timeout: 15_000 },
  );
  await expect(page.locator(".message.user").last()).toContainText("Compact implementation plan");

  const nodes = await fetchRuntimeNodes(page, sidebar.session_id);
  expect(nodes).toHaveLength(4);
  const root = nodes.find((node) => !isRuntimeTurnResponse(node));
  expect(root).toEqual({
    session_id: sidebar.session_id,
    thread_id: sidebar.session_id,
    id: expect.stringMatching(/^turn_/),
  });
  const turns = nodes.filter(isRuntimeTurnResponse);
  expect(turns).toHaveLength(3);
  const plan = turns.find((turn) => turn.parent_id === root?.id);
  expect(plan).toBeDefined();
  const compact = turns.find((turn) => turn.parent_id === plan?.id);
  expect(compact).toBeDefined();
  const agent = turns.find((turn) => turn.parent_id === compact?.id);
  expect(agent).toBeDefined();
  expect([plan?.running_mode, compact?.running_mode, agent?.running_mode]).toEqual(["plan", "plan", "agent"]);
  expect([plan?.status, compact?.status, agent?.status]).toEqual(["success", "success", "success"]);
  expect(compact?.data[compact.current_data_idx][1].content).toEqual(
    expect.arrayContaining([expect.objectContaining({ type: "compaction" })]),
  );
  expect(agent?.data[agent.current_data_idx][0].content).toEqual([
    {
      type: "text",
      text: "<approved_plan>\n# Compact implementation plan\n\n1. Preserve the reviewed Plan Turn.\n2. Compact the conversation context.\n3. Implement from this exact plan text.\n</approved_plan>",
      status: "success",
    },
  ]);
});

test("refresh reattaches a running Turn and flushes the persisted queue as one message", async ({ page }) => {
  const sidebarResponse = await page.request.post("/api/sidebar-threads", {
    data: { title: "Reconnect Queue" },
  });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { session_id: string };

  await page.goto("/app");
  await page.getByRole("button", { name: "Reconnect Queue", exact: true }).click();
  await page.getByLabel("聊天输入").fill("delayed reconnect");
  const createResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await createResponse).ok()).toBeTruthy();
  await expect(page.locator(".message.assistant").last()).toContainText(
    "Streaming began before refresh.",
    { timeout: 15_000 },
  );

  await page.getByLabel("聊天输入").fill("queued first");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await page.getByLabel("聊天输入").fill("queued second");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("region", { name: "待发送消息" })).toContainText("待发送 2 条");

  await page.reload();
  await expect(page.getByLabel("聊天输入")).toBeVisible();
  await expect(page.getByRole("region", { name: "待发送消息" })).toContainText("待发送 2 条");
  await expect(page.locator(".message.assistant").first()).toContainText(
    "Streaming finished after refresh.",
    { timeout: 15_000 },
  );
  await expect(page.getByRole("region", { name: "待发送消息" })).toHaveCount(0, { timeout: 15_000 });
  await expect(page.locator(".message.user")).toHaveCount(2, { timeout: 15_000 });
  await expect(page.locator(".message.user").last()).toContainText("queued first");
  await expect(page.locator(".message.user").last()).toContainText("queued second");

  await expect.poll(async () => {
    const turns = (await fetchRuntimeNodes(page, sidebar.session_id)).filter(isRuntimeTurnResponse);
    return turns.map((turn) => turn.data[turn.current_data_idx][0].content
      .filter((item) => item.type === "text")
      .map((item) => item.text ?? "")
      .join(""));
  }, { timeout: 15_000 }).toEqual(["delayed reconnect", "queued first\n\nqueued second"]);
});

test("a paused Turn resumes in place with the same id", async ({ page }) => {
  const sidebarResponse = await page.request.post("/api/sidebar-threads", { data: { title: "Pause Resume" } });
  expect(sidebarResponse.ok(), String(sidebarResponse.status())).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { session_id: string };

  await page.goto("/app");
  await page.getByRole("button", { name: "Pause Resume", exact: true }).click();
  await page.getByLabel("聊天输入").fill("pause and resume");
  const createResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送" }).click();
  expect((await createResponse).ok()).toBeTruthy();
  await expect(page.getByRole("button", { name: "暂停" })).toBeVisible();
  await page.getByRole("button", { name: "暂停" }).click();
  await expect(page.getByRole("button", { name: "继续" })).toBeVisible({ timeout: 15_000 });
  await expect(page.locator(".message.assistant").last()).toContainText("Partial output preserved before pause.");
  await expect(page.getByText("The run was paused at the user's request.", { exact: false })).toHaveCount(0);

  const pausedTurns = (await fetchRuntimeNodes(page, sidebar.session_id)).filter(isRuntimeTurnResponse);
  expect(pausedTurns).toHaveLength(1);
  expect(pausedTurns[0].status).toBe("paused");
  const pausedItems = pausedTurns[0].data[pausedTurns[0].current_data_idx][1].content;
  expect(pausedItems).toContainEqual(expect.objectContaining({
    type: "text",
    text: "Partial output preserved before pause.",
    status: "failed",
  }));
  expect(pausedItems.some((item) => item.type === "error")).toBe(false);
  const originalTurnId = pausedTurns[0].id;

  const resumeResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith(`/${originalTurnId}/resume`),
  );
  await page.getByRole("button", { name: "继续" }).click();
  expect((await resumeResponse).ok()).toBeTruthy();
  await expect(page.getByText("恢复运行", { exact: true })).toHaveCount(0);

  await expect(page.locator(".message.assistant").last()).toContainText("Resumed the same Turn successfully.", { timeout: 15_000 });
  await expect(page.getByRole("button", { name: "发送" })).toBeVisible();
  const resumedTurns = (await fetchRuntimeNodes(page, sidebar.session_id)).filter(isRuntimeTurnResponse);
  expect(resumedTurns).toHaveLength(1);
  expect(resumedTurns[0]).toMatchObject({ id: originalTurnId, status: "success" });
});

test("running Turn consumes FIFO steering as separate user Messages", async ({ page }) => {
  const sidebarResponse = await page.request.post("/api/sidebar-threads", { data: { title: "FIFO Steering" } });
  expect(sidebarResponse.ok(), String(sidebarResponse.status())).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { session_id: string };

  await page.goto("/app");
  await page.getByRole("button", { name: "FIFO Steering", exact: true }).click();
  await page.getByLabel("聊天输入").fill("steering fifo");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("button", { name: "暂停" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Fork" })).toHaveCount(0);

  await page.getByLabel("聊天输入").fill("first redirect");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await page.getByLabel("聊天输入").fill("second redirect");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("region", { name: "待发送消息" })).toContainText("待发送 2 条");

  const firstSteer = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/steer"));
  await page.getByRole("button", { name: "发送第 1 条待发送消息" }).click();
  expect((await firstSteer).status()).toBe(202);
  const secondSteer = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/steer"));
  await page.getByRole("button", { name: "发送第 2 条待发送消息" }).click();
  expect((await secondSteer).status()).toBe(202);

  await expect(page.getByRole("region", { name: "待发送消息" })).toHaveCount(0, { timeout: 15_000 });
  await expect(page.locator(".message.user")).toHaveCount(3, { timeout: 15_000 });
  await expect(page.locator(".message.user").nth(1)).toContainText("first redirect");
  await expect(page.locator(".message.user").nth(2)).toContainText("second redirect");
  await expect(page.locator(".message.assistant").last()).toContainText("FIFO steering complete.");
  await expect(page.getByRole("button", { name: "Fork" })).toBeVisible();

  const turns = (await fetchRuntimeNodes(page, sidebar.session_id)).filter(isRuntimeTurnResponse);
  expect(turns).toHaveLength(1);
  expect(turns[0].data[turns[0].current_data_idx].map((message) => message.role)).toEqual([
    "user", "assistant", "user", "assistant", "user", "assistant",
  ]);
});

test("steering waits for the active tool and skips the next stale tool", async ({ page }) => {
  const sidebarResponse = await page.request.post("/api/sidebar-threads", { data: { title: "Tool Steering" } });
  expect(sidebarResponse.ok(), String(sidebarResponse.status())).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { session_id: string };

  await page.goto("/app");
  await page.getByRole("button", { name: "Tool Steering", exact: true }).click();
  await page.getByLabel("聊天输入").fill("steering during tool");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator(".message.assistant").last()).toContainText("slow_tool", { timeout: 15_000 });

  await page.getByLabel("聊天输入").fill("redirect after slow tool");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const steerResponse = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/steer"));
  await page.getByRole("button", { name: "发送第 1 条待发送消息" }).click();
  expect((await steerResponse).status()).toBe(202);

  await expect(page.locator(".message.assistant").last()).toContainText("Tool-boundary steering complete.", { timeout: 15_000 });
  await expect(page.getByText("Forbidden tool executed.", { exact: false })).toHaveCount(0);
  await expect(page.locator(".message.user")).toHaveCount(2);

  const turns = (await fetchRuntimeNodes(page, sidebar.session_id)).filter(isRuntimeTurnResponse);
  const firstAssistant = turns[0].data[turns[0].current_data_idx][1].content;
  expect(firstAssistant).toContainEqual(expect.objectContaining({
    type: "tool_result",
    call_id: "slow_steering",
    content: "Slow tool completed.",
    status: "success",
  }));
  expect(firstAssistant).toContainEqual(expect.objectContaining({
    type: "tool_result",
    call_id: "forbidden_steering",
    status: "failed",
  }));
});

test("Pause merges the local queue into one same-Turn steering Message", async ({ page }) => {
  let pauseRequests = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/pause")) pauseRequests += 1;
  });
  const sidebarResponse = await page.request.post("/api/sidebar-threads", { data: { title: "Merged Steering" } });
  expect(sidebarResponse.ok(), String(sidebarResponse.status())).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { session_id: string };

  await page.goto("/app");
  await page.getByRole("button", { name: "Merged Steering", exact: true }).click();
  await page.getByLabel("聊天输入").fill("steering merge");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("button", { name: "暂停" })).toBeVisible();
  await page.getByLabel("聊天输入").fill("merge first");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await page.getByLabel("聊天输入").fill("merge second");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  const steerResponse = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/steer"));
  await page.getByRole("button", { name: "暂停" }).click();
  expect((await steerResponse).status()).toBe(202);
  expect(pauseRequests).toBe(0);
  await expect(page.getByRole("button", { name: "继续" })).toHaveCount(0);
  await expect(page.locator(".message.user")).toHaveCount(2, { timeout: 15_000 });
  await expect(page.locator(".message.user").last()).toContainText("merge first");
  await expect(page.locator(".message.user").last()).toContainText("merge second");
  await expect(page.locator(".message.assistant").last()).toContainText("Merged steering complete.");

  const turns = (await fetchRuntimeNodes(page, sidebar.session_id)).filter(isRuntimeTurnResponse);
  expect(turns).toHaveLength(1);
  expect(turns[0].status).toBe("success");
  expect(turns[0].data[turns[0].current_data_idx].map((message) => message.role)).toEqual([
    "user", "assistant", "user", "assistant",
  ]);
});

test("assistant Items stay chronological and runtime Collapse starts folded", async ({ page }) => {
  const sidebar = await page.request.post("/api/sidebar-threads", { data: { title: "Ordered Items" } });
  expect(sidebar.ok(), `${sidebar.status()} ${await sidebar.text()}`).toBeTruthy();

  await page.goto("/app");
  await page.getByRole("button", { name: "Ordered Items", exact: true }).click();
  await page.getByLabel("聊天输入").fill("ordered items");
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送" }).click();

  const assistant = page.locator(".message.assistant").last();
  const firstReasoning = assistant.locator('.runtime-item-collapse[data-item-type="reasoning"]').first();
  await expect(firstReasoning.locator(".ant-collapse-item")).not.toHaveClass(/ant-collapse-item-active/);
  await expect(firstReasoning.locator(".runtime-summary-text")).toContainText("右侧最新字符可见");
  await expect(firstReasoning.locator(".runtime-status-dot")).toHaveCount(0);
  await expect(firstReasoning.locator(".shimmer-text.is-active")).toHaveCount(0);

  const activeTool = assistant.locator('.runtime-item-collapse[data-item-type="tool_call"]').first();
  await expect(activeTool.locator(".ant-collapse-header")).toContainText("正在调用 slow_tool", { timeout: 15_000 });
  await expect(activeTool.locator(".ant-collapse-item")).not.toHaveClass(/ant-collapse-item-active/);
  await expect(activeTool.locator(".runtime-status-dot")).toHaveCount(3);
  const toolShimmer = activeTool.locator(".shimmer-text.is-active");
  await expect(toolShimmer).toHaveText("正在调用 slow_tool");
  expect(await toolShimmer.evaluate((element) => element.getAnimations()
    .filter((animation) => (animation as CSSAnimation).animationName === "runtime-summary-shimmer").length)).toBe(1);
  expect(await activeTool.locator(".runtime-status-dot").evaluateAll((dots) => dots.filter((dot) => dot.getAnimations()
    .some((animation) => (animation as CSSAnimation).animationName === "runtime-status-dot")).length)).toBe(3);
  await activeTool.locator(".ant-collapse-header").click();
  await expect(activeTool.locator(".ant-collapse-item")).toHaveClass(/ant-collapse-item-active/);
  await expect(activeTool.locator(".shimmer-text.is-active")).toHaveCount(0);
  await activeTool.locator(".ant-collapse-header").click();
  await expect(activeTool.locator(".ant-collapse-item")).not.toHaveClass(/ant-collapse-item-active/);
  await expect(activeTool.locator(".shimmer-text.is-active")).toHaveCount(1);
  expect((await responsePromise).ok()).toBeTruthy();
  await expect(assistant).toContainText("Ordered flow complete.", { timeout: 15_000 });

  const itemTypes = await assistant.locator(".runtime-items > [data-item-type]").evaluateAll((items) =>
    items.map((item) => (item as HTMLElement).dataset.itemType),
  );
  expect(itemTypes).toEqual([
    "reasoning",
    "tool_call",
    "tool_result",
    "text",
    "tool_call",
    "tool_result",
    "reasoning",
    "text",
  ]);
  await expect(assistant.locator(".runtime-item-collapse .ant-collapse-item-active")).toHaveCount(0);
  await expect(assistant.locator(".shimmer-text.is-active")).toHaveCount(0);

  const summaryViewport = firstReasoning.locator(".runtime-summary-viewport");
  await expect(summaryViewport).toBeVisible();
  const summaryMetrics = await summaryViewport.evaluate((element) => {
    const viewport = element as HTMLElement;
    const rect = viewport.getBoundingClientRect();
    return {
      left: rect.left,
      right: rect.right,
      clientWidth: viewport.clientWidth,
      scrollWidth: viewport.scrollWidth,
      scrollLeft: viewport.scrollLeft,
    };
  });
  expect(summaryMetrics.scrollWidth).toBeGreaterThan(summaryMetrics.clientWidth);
  expect(Math.abs(summaryMetrics.scrollLeft - (summaryMetrics.scrollWidth - summaryMetrics.clientWidth))).toBeLessThanOrEqual(1);
  await expect(summaryViewport.locator(".shimmer-text")).toHaveCount(0);

  await firstReasoning.locator(".ant-collapse-header").click();
  await expect(firstReasoning.locator(".ant-collapse-item")).toHaveClass(/ant-collapse-item-active/);
  const reasoningBody = firstReasoning.locator(".thinking-content");
  await expect(reasoningBody).toBeVisible();
  const bodyBounds = await reasoningBody.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { left: rect.left, right: rect.right };
  });
  expect(Math.abs(summaryMetrics.left - bodyBounds.left)).toBeLessThanOrEqual(1);
  expect(Math.abs(summaryMetrics.right - bodyBounds.right)).toBeLessThanOrEqual(1);
  const reasoningItem = firstReasoning.locator(".ant-collapse-item");
  await expect(async () => {
    if (await reasoningItem.evaluate((element) => element.classList.contains("ant-collapse-item-active"))) {
      await firstReasoning.locator(".ant-collapse-header").click();
    }
    await expect(reasoningItem).not.toHaveClass(/ant-collapse-item-active/, { timeout: 1_000 });
  }).toPass({ timeout: 5_000 });

  await expect(assistant.getByText("The first tool completed.", { exact: false })).toBeVisible();
  expect(await assistant.getByText("The first tool completed.", { exact: false }).evaluate((element) => element.closest(".runtime-collapse"))).toBeNull();
});

test("tool approval shows one pending card and one allowed status", async ({ page }) => {
  const sidebarResponse = await page.request.post("/api/sidebar-threads", { data: { title: "Approval Allowed" } });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();
  const sidebar = await sidebarResponse.json() as { session_id: string };

  await page.goto("/app");
  await page.getByRole("button", { name: "Approval Allowed", exact: true }).click();
  await page.getByLabel("聊天输入").fill("approval presentation");
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送" }).click();
  expect((await responsePromise).ok()).toBeTruthy();

  const assistant = page.locator(".message.assistant").last();
  await expect(assistant.getByText("Call tool web_search?", { exact: true })).toHaveCount(1);
  await expect(assistant.locator(".decision-card")).toHaveCount(1);
  await expect(page.getByText("none", { exact: true })).toHaveCount(0);
  await assistant.getByRole("button", { name: "本次允许" }).click();

  await expect(assistant.getByText("已允许 web_search", { exact: true })).toHaveCount(1);
  await expect(assistant.getByText("Call tool web_search?", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "发送" })).toBeVisible({ timeout: 15_000 });

  const turns = (await fetchRuntimeNodes(page, sidebar.session_id)).filter(isRuntimeTurnResponse);
  const content = turns[0].data[turns[0].current_data_idx][1].content;
  expect(content.filter((item) => item.type === "approval")).toEqual([
    expect.objectContaining({ event: "decision_requested", call_id: "approval_search", tool: "web_search" }),
  ]);
});

test("denied tool approval shows one static denied status", async ({ page }) => {
  const sidebarResponse = await page.request.post("/api/sidebar-threads", { data: { title: "Approval Denied" } });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();

  await page.goto("/app");
  await page.getByRole("button", { name: "Approval Denied", exact: true }).click();
  await page.getByLabel("聊天输入").fill("approval presentation");
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await page.getByRole("button", { name: "发送" }).click();
  expect((await responsePromise).ok()).toBeTruthy();

  const assistant = page.locator(".message.assistant").last();
  await expect(assistant.getByText("Call tool web_search?", { exact: true })).toHaveCount(1);
  await assistant.getByRole("button", { name: "拒绝" }).click();

  await expect(assistant.getByText("已拒绝 web_search", { exact: true })).toHaveCount(1);
  await expect(assistant.getByText("Call tool web_search?", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "发送" })).toBeVisible({ timeout: 15_000 });
});

test("Sandbox health gate recovers through the real status and repair HTTP flow", async ({ page }) => {
  await page.goto("/app");
  const editor = page.getByLabel("聊天输入");
  await expect(editor).toHaveAttribute("contenteditable", "true");

  const unhealthy = await page.request.post("/api/test/sandbox-status", {
    data: { installed: true, healthy: false, detail: "E2E Broker service stopped" },
  });
  expect(unhealthy.ok(), `${unhealthy.status()} ${await unhealthy.text()}`).toBeTruthy();

  await page.getByRole("button", { name: /个人简介：/ }).click();
  await page.getByRole("menuitem", { name: "沙箱" }).click();
  const statusResponse = page.waitForResponse((response) =>
    response.request().method() === "GET" && response.url().endsWith("/api/sandbox/status"),
  );
  await page.getByRole("button", { name: /检\s*查/ }).click();
  expect((await statusResponse).ok()).toBeTruthy();
  await expect(page.getByText("E2E Broker service stopped", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /修\s*复/ })).toBeVisible();

  const repairResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/sandbox/repair"),
  );
  await page.getByRole("button", { name: /修\s*复/ }).click();
  expect((await repairResponse).ok()).toBeTruthy();
  await expect(page.getByText("E2E Broker service stopped", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /修\s*复/ })).toHaveCount(0);
  await expect(page.getByText("沙箱已就绪", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.locator(".sandbox-health-failure")).toHaveCount(0);
  await expect(editor).toHaveAttribute("contenteditable", "true");
});
