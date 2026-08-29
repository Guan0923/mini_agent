import { expect, test, type Locator, type Page } from "@playwright/test";

interface SidebarThreadResponse {
  session_id: string;
}

interface RuntimeTurnResponse {
  id: string;
  session_id: string;
  thread_id: string;
  cwd?: string | null;
  data: unknown[][];
}

async function createConversation(page: Page, title: string): Promise<SidebarThreadResponse> {
  const response = await page.request.post("/api/sidebar-threads", { data: { title } });
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  return response.json() as Promise<SidebarThreadResponse>;
}

async function sendMessage(scope: Locator | Page, page: Page, text: string): Promise<void> {
  const editor = scope.getByLabel("聊天输入");
  await editor.fill(text);
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/api/turns"),
  );
  await scope.getByRole("button", { name: "发送", exact: true }).click();
  const response = await responsePromise;
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  await expect(scope.locator(".message.user").last()).toContainText(text, { timeout: 15_000 });
  await expect(scope.getByRole("button", { name: "发送", exact: true })).toBeVisible({ timeout: 15_000 });
}

async function openCreationMenu(page: Page): Promise<void> {
  await page.getByRole("button", { name: "打开右侧边栏菜单" }).click();
}

test("side chat hides its anchor history, survives refresh, and leaves choices after its last tab closes", async ({ page }) => {
  const title = "Right Panel Side Chat E2E";
  const { session_id: sessionId } = await createConversation(page, title);

  await page.goto("/app");
  await page.getByRole("button", { name: title, exact: true }).click();
  await sendMessage(page, page, "main history must stay hidden from side chat");

  await openCreationMenu(page);
  const createResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().includes("/api/right-panel/")
      && response.url().endsWith("/side-chats"),
  );
  await page.getByRole("menuitem", { name: /侧边聊天/ }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.ok(), `${createResponse.status()} ${await createResponse.text()}`).toBeTruthy();

  const panel = page.locator(".right-panel-side-chat");
  await expect(panel).toBeVisible();
  await expect(panel.locator(".message")).toHaveCount(0);
  await expect(panel).not.toContainText("main history must stay hidden from side chat");

  await sendMessage(panel, page, "hello from the isolated side chat");
  await expect(panel.locator(".message.assistant").last()).toContainText("Hello! I can help", { timeout: 15_000 });
  await expect(panel.getByRole("button", { name: "Fork" })).toHaveCount(0);

  const sidebarResponse = await page.request.get("/api/sidebar-threads?status=active");
  expect(sidebarResponse.ok(), `${sidebarResponse.status()} ${await sidebarResponse.text()}`).toBeTruthy();
  const sidebarThreads = await sidebarResponse.json() as Array<{ session_id: string; title: string }>;
  expect(sidebarThreads.filter((item) => item.session_id === sessionId)).toHaveLength(1);

  await page.reload();
  const restoredPanel = page.locator(".right-panel-side-chat");
  await expect(restoredPanel).toBeVisible({ timeout: 15_000 });
  await expect(restoredPanel.locator(".message.user").last()).toContainText("hello from the isolated side chat");
  await expect(restoredPanel.locator(".message.assistant").last()).toContainText("Hello! I can help");
  await expect(restoredPanel).not.toContainText("main history must stay hidden from side chat");

  const deleteResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "DELETE" && response.url().includes("/api/right-panel/"),
  );
  await page.locator(".right-panel-tabs .ant-tabs-tab-remove").click();
  const deleteResponse = await deleteResponsePromise;
  expect(deleteResponse.ok(), `${deleteResponse.status()} ${await deleteResponse.text()}`).toBeTruthy();
  await expect(page.getByRole("button", { name: /创建侧边聊天/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开终端/ })).toBeVisible();
  await expect(page.locator(".right-panel-shell")).toBeVisible();

  await page.reload();
  await expect(page.getByRole("button", { name: /创建侧边聊天/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开终端/ })).toBeVisible();
  await expect(page.locator(".right-panel-shell")).toBeVisible();
});

test("mobile right panel uses a full-width Drawer and keeps the empty creation state", async ({ page }) => {
  await page.setViewportSize({ width: 480, height: 800 });
  const title = "Right Panel Mobile E2E";
  await createConversation(page, title);

  await page.goto("/app");
  await page.getByRole("button", { name: "打开会话列表" }).click();
  await page.getByRole("button", { name: title, exact: true }).click();
  await sendMessage(page, page, "create a Turn before opening the mobile panel");

  await openCreationMenu(page);
  await page.getByRole("menuitem", { name: "打开右栏", exact: true }).click();

  const drawer = page.locator(".ant-drawer-content-wrapper:visible");
  await expect(drawer).toBeVisible();
  await expect(page.getByRole("dialog", { name: "右侧边栏" })).toBeVisible();
  await expect(page.getByRole("button", { name: /创建侧边聊天/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开终端/ })).toBeVisible();
  await expect.poll(async () => (await drawer.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(479);

  await drawer.locator(".ant-drawer-close").click();
  await expect(drawer).toHaveCount(0);
  await expect(page.getByRole("button", { name: "打开右侧边栏菜单" })).toBeVisible();
});

test("real cmd terminal starts in the Turn cwd, replays after refresh, and closes from its tab", async ({ page }) => {
  test.slow();
  const title = "Right Panel Terminal E2E";
  const { session_id: sessionId } = await createConversation(page, title);

  await page.goto("/app");
  await page.getByRole("button", { name: title, exact: true }).click();
  await sendMessage(page, page, "create the terminal source Turn");

  const turnsResponse = await page.request.get(`/api/turns?session_id=${encodeURIComponent(sessionId)}`);
  expect(turnsResponse.ok(), `${turnsResponse.status()} ${await turnsResponse.text()}`).toBeTruthy();
  const turns = (await turnsResponse.json() as RuntimeTurnResponse[]).filter((item) => "data" in item);
  const sourceTurn = turns.find((item) => item.thread_id === sessionId);
  expect(sourceTurn?.cwd).toBeTruthy();

  await openCreationMenu(page);
  const createResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().includes("/api/right-panel/")
      && response.url().endsWith("/terminals"),
  );
  await page.getByRole("menuitem", { name: /终端/ }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.ok(), `${createResponse.status()} ${await createResponse.text()}`).toBeTruthy();

  const terminalHost = page.locator(".right-panel-terminal-host");
  await expect(terminalHost.locator(".xterm")).toBeVisible({ timeout: 15_000 });
  const terminalRows = terminalHost.locator(".xterm-rows");
  const input = terminalHost.locator(".xterm-helper-textarea");
  await input.focus();
  await input.pressSequentially("echo __RIGHT_PANEL_CWD__%CD%");
  await input.press("Enter");
  await expect(terminalRows).toContainText("__RIGHT_PANEL_CWD__", { timeout: 15_000 });
  await expect(terminalRows).toContainText(sourceTurn!.cwd!, { timeout: 15_000 });

  await page.reload();
  const restoredHost = page.locator(".right-panel-terminal-host");
  await expect(restoredHost.locator(".xterm")).toBeVisible({ timeout: 15_000 });
  await expect(restoredHost.locator(".xterm-rows")).toContainText("__RIGHT_PANEL_CWD__", { timeout: 15_000 });
  const restoredInput = restoredHost.locator(".xterm-helper-textarea");
  await restoredInput.focus();
  await restoredInput.pressSequentially("echo __RIGHT_PANEL_RECONNECTED__");
  await restoredInput.press("Enter");
  await expect(restoredHost.locator(".xterm-rows")).toContainText("__RIGHT_PANEL_RECONNECTED__", { timeout: 15_000 });

  const deleteResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "DELETE" && response.url().includes("/api/right-panel/"),
  );
  await page.locator(".right-panel-tabs .ant-tabs-tab-remove").click();
  const deleteResponse = await deleteResponsePromise;
  expect(deleteResponse.ok(), `${deleteResponse.status()} ${await deleteResponse.text()}`).toBeTruthy();
  await expect(page.getByRole("button", { name: /创建侧边聊天/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开终端/ })).toBeVisible();

  const panelResponse = await page.request.get(`/api/right-panel/${encodeURIComponent(sessionId)}`);
  expect(panelResponse.ok(), `${panelResponse.status()} ${await panelResponse.text()}`).toBeTruthy();
  expect((await panelResponse.json() as { windows: unknown[] }).windows).toHaveLength(0);
});
