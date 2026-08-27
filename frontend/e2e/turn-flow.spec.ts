import { expect, test } from "@playwright/test";

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

test("real Turn SSE flow supports tools, rewind versions, fork, and compact", async ({ page }) => {
  const guest = await page.request.post("/api/auth/guest");
  expect(guest.ok(), `${guest.status()} ${await guest.text()}`).toBeTruthy();
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
  await expect(page.getByRole("button", { name: "Playwright Turn", exact: true })).toHaveCount(2);
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
  const guest = await page.request.post("/api/auth/guest");
  expect(guest.ok(), `${guest.status()} ${await guest.text()}`).toBeTruthy();
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

  const turnsResponse = await page.request.get(
    `/api/turns?session_id=${encodeURIComponent(sidebar.session_id)}`,
  );
  expect(turnsResponse.ok(), `${turnsResponse.status()} ${await turnsResponse.text()}`).toBeTruthy();
  const turns = await turnsResponse.json() as Array<{
    id: string;
    parent_id: string;
    running_mode: "agent" | "plan";
    status: string;
    current_data_idx: number;
    data: Array<Array<{ role: string; content: Array<Record<string, unknown>> }>>;
  }>;
  expect(turns).toHaveLength(3);
  const plan = turns.find((turn) => !turn.parent_id);
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
      text: "# Compact implementation plan\n\n1. Preserve the reviewed Plan Turn.\n2. Compact the conversation context.\n3. Implement from this exact plan text.",
    },
  ]);
});

test("refresh reattaches a running Turn and flushes the persisted queue as one message", async ({ page }) => {
  const guest = await page.request.post("/api/auth/guest");
  expect(guest.ok(), `${guest.status()} ${await guest.text()}`).toBeTruthy();
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
    const response = await page.request.get(
      `/api/turns?session_id=${encodeURIComponent(sidebar.session_id)}`,
    );
    const turns = await response.json() as Array<{
      data: Array<Array<{ role: string; content: Array<{ type: string; text?: string }> }>>;
      current_data_idx: number;
    }>;
    return turns.map((turn) => turn.data[turn.current_data_idx][0].content
      .filter((item) => item.type === "text")
      .map((item) => item.text ?? "")
      .join(""));
  }, { timeout: 15_000 }).toEqual(["delayed reconnect", "queued first\n\nqueued second"]);
});

test("a paused Turn resumes in place with the same id", async ({ page }) => {
  const guest = await page.request.post("/api/auth/guest");
  expect(guest.ok(), `${guest.status()} ${await guest.text()}`).toBeTruthy();
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

  const pausedTurnsResponse = await page.request.get(`/api/turns?session_id=${encodeURIComponent(sidebar.session_id)}`);
  const pausedTurns = await pausedTurnsResponse.json() as Array<{ id: string; status: string }>;
  expect(pausedTurns).toHaveLength(1);
  expect(pausedTurns[0].status).toBe("paused");
  const originalTurnId = pausedTurns[0].id;

  const resumeResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith(`/${originalTurnId}/resume`),
  );
  await page.getByRole("button", { name: "继续" }).click();
  expect((await resumeResponse).ok()).toBeTruthy();
  await expect(page.getByText("恢复运行", { exact: true })).toHaveCount(0);

  await expect(page.locator(".message.assistant").last()).toContainText("Resumed the same Turn successfully.", { timeout: 15_000 });
  await expect(page.getByRole("button", { name: "发送" })).toBeVisible();
  const resumedTurnsResponse = await page.request.get(`/api/turns?session_id=${encodeURIComponent(sidebar.session_id)}`);
  const resumedTurns = await resumedTurnsResponse.json() as Array<{ id: string; status: string }>;
  expect(resumedTurns).toHaveLength(1);
  expect(resumedTurns[0]).toMatchObject({ id: originalTurnId, status: "success" });
});

test("running Turn consumes FIFO steering as separate user Messages", async ({ page }) => {
  const guest = await page.request.post("/api/auth/guest");
  expect(guest.ok(), `${guest.status()} ${await guest.text()}`).toBeTruthy();
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

  const turnsResponse = await page.request.get(`/api/turns?session_id=${encodeURIComponent(sidebar.session_id)}`);
  const turns = await turnsResponse.json() as Array<{ current_data_idx: number; data: Array<Array<{ role: string; steering_id?: string }>> }>;
  expect(turns).toHaveLength(1);
  expect(turns[0].data[turns[0].current_data_idx].map((message) => message.role)).toEqual([
    "user", "assistant", "user", "assistant", "user", "assistant",
  ]);
});

test("steering waits for the active tool and skips the next stale tool", async ({ page }) => {
  const guest = await page.request.post("/api/auth/guest");
  expect(guest.ok(), `${guest.status()} ${await guest.text()}`).toBeTruthy();
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

  const turnsResponse = await page.request.get(`/api/turns?session_id=${encodeURIComponent(sidebar.session_id)}`);
  const turns = await turnsResponse.json() as Array<{
    current_data_idx: number;
    data: Array<Array<{ role: string; content: Array<Record<string, unknown>> }>>;
  }>;
  const firstAssistant = turns[0].data[turns[0].current_data_idx][1].content;
  expect(firstAssistant).toContainEqual(expect.objectContaining({
    type: "tool_result",
    call_id: "slow_steering",
    content: "Slow tool completed.",
    status: "succeeded",
  }));
  expect(firstAssistant).toContainEqual(expect.objectContaining({
    type: "tool_result",
    call_id: "forbidden_steering",
    status: "failed",
  }));
});

test("Pause merges the local queue into one same-Turn steering Message", async ({ page }) => {
  const guest = await page.request.post("/api/auth/guest");
  expect(guest.ok(), `${guest.status()} ${await guest.text()}`).toBeTruthy();
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
  await expect(page.getByRole("button", { name: "继续" })).toHaveCount(0);
  await expect(page.locator(".message.user")).toHaveCount(2, { timeout: 15_000 });
  await expect(page.locator(".message.user").last()).toContainText("merge first");
  await expect(page.locator(".message.user").last()).toContainText("merge second");
  await expect(page.locator(".message.assistant").last()).toContainText("Merged steering complete.");

  const turnsResponse = await page.request.get(`/api/turns?session_id=${encodeURIComponent(sidebar.session_id)}`);
  const turns = await turnsResponse.json() as Array<{ status: string; current_data_idx: number; data: Array<Array<{ role: string }>> }>;
  expect(turns).toHaveLength(1);
  expect(turns[0].status).toBe("success");
  expect(turns[0].data[turns[0].current_data_idx].map((message) => message.role)).toEqual([
    "user", "assistant", "user", "assistant",
  ]);
});

test("assistant Items stay chronological and runtime Collapse starts folded", async ({ page }) => {
  const guest = await page.request.post("/api/auth/guest");
  expect(guest.ok(), `${guest.status()} ${await guest.text()}`).toBeTruthy();
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
  await firstReasoning.locator(".ant-collapse-header").click();
  await expect(firstReasoning.locator(".ant-collapse-item")).not.toHaveClass(/ant-collapse-item-active/);

  await expect(assistant.getByText("The first tool completed.", { exact: false })).toBeVisible();
  expect(await assistant.getByText("The first tool completed.", { exact: false }).evaluate((element) => element.closest(".runtime-collapse"))).toBeNull();
});

test("tool approval shows one pending card and one allowed status", async ({ page }) => {
  const guest = await page.request.post("/api/auth/guest");
  expect(guest.ok(), `${guest.status()} ${await guest.text()}`).toBeTruthy();
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

  const turnsResponse = await page.request.get(`/api/turns?session_id=${encodeURIComponent(sidebar.session_id)}`);
  const turns = await turnsResponse.json() as Array<{ current_data_idx: number; data: Array<Array<{ content: Array<Record<string, unknown>> }>> }>;
  const content = turns[0].data[turns[0].current_data_idx][1].content;
  expect(content.filter((item) => item.type === "approval")).toEqual([
    expect.objectContaining({ event: "decision_requested", call_id: "approval_search", tool: "web_search" }),
  ]);
});

test("denied tool approval shows one static denied status", async ({ page }) => {
  const guest = await page.request.post("/api/auth/guest");
  expect(guest.ok(), `${guest.status()} ${await guest.text()}`).toBeTruthy();
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
