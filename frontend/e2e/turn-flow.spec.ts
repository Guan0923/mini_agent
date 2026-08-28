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
  current_data_idx: number;
  data: Array<Array<{
    role: string;
    steering_id?: string;
    content: Array<Record<string, unknown> & { type: string; text?: string }>;
  }>>;
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
  await expect(page.getByRole("button", { name: "发送" })).toBeVisible({ timeout: 15_000 });
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

test("Trace audit expands real HTTP model, preference, Skill, MCP schema, and Turn Items", async ({ page }) => {
  const sidebarResponse = await page.request.post("/api/sidebar-threads", { data: { title: "Trace Audit E2E" } });
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();

  await page.goto("/app");
  await page.getByRole("button", { name: "Trace Audit E2E", exact: true }).click();
  await send(page, "$trace-audit trace audit e2e");
  await expect(page.locator(".message.assistant").last()).toContainText("Trace response from HTTP.");

  await page.getByRole("button", { name: "Trace", exact: true }).click();
  await expect(page.getByLabel("聊天输入")).toHaveCount(0);
  await expect(page.getByText("Preference", { exact: true })).toBeVisible();
  await expect(page.getByText("Skill", { exact: true })).toBeVisible();
  await expect(page.getByText("MCP", { exact: true })).toBeVisible();

  const preference = tracePanel(page, "Preference");
  await preference.locator(".ant-collapse-header").click();
  await expect(preference.locator(".trace-value")).toContainText("Trace E2E preference: concise local audit.");

  const skill = tracePanel(page, "Skill");
  await skill.locator(".ant-collapse-header").click();
  await expect(skill.locator(".trace-value")).toContainText("complete local Skill instructions");
  await expect(skill.locator(".trace-value")).toContainText('"source": "user"');

  const mcp = tracePanel(page, "MCP");
  await mcp.locator(".ant-collapse-header").click();
  await expect(mcp.locator(".trace-value")).toContainText('"server": "trace"');
  await expect(mcp.locator(".trace-value")).toContainText('"tool": "inspect_trace"');

  const effectiveSystem = tracePanel(page, "System", 1);
  await effectiveSystem.locator(".ant-collapse-header").click();
  await expect(effectiveSystem.locator(".trace-value")).toContainText("Active project Skills");
  await expect(effectiveSystem.locator(".trace-value")).toContainText("User Agent Preferences");

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
  await send(page, "请生成这个对话的模型标题");

  await expect(page.getByRole("button", { name: "浏览器生成的新标题很", exact: true })).toBeVisible();
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

  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.locator(".sandbox-health-failure")).toHaveCount(0);
  await expect(editor).toHaveAttribute("contenteditable", "true");
});
