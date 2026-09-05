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
  const assistantFrame = scope.locator(".message.assistant").last().locator(".assistant-run-frame");
  await expect(assistantFrame).toBeVisible({ timeout: 15_000 });
  await expect(assistantFrame).not.toHaveClass(/is-running/, { timeout: 15_000 });
}

async function openRightPanel(page: Page): Promise<void> {
  await page.getByRole("button", { name: "打开右侧边栏" }).click();
}

async function expectEmptyHorizontallyCentered(shell: Locator): Promise<void> {
  const layout = await shell.evaluate((element) => {
    const empty = element.querySelector<HTMLElement>(".right-panel-empty");
    const placeholder = element.querySelector<HTMLElement>(".right-panel-empty .ant-empty");
    const shellBox = element.getBoundingClientRect();
    const emptyBox = empty?.getBoundingClientRect();
    const placeholderBox = placeholder?.getBoundingClientRect();
    const center = (box: DOMRect) => box.left + box.width / 2;
    const missingElementGap = Number.MAX_SAFE_INTEGER;
    return {
      emptyWidthGap: emptyBox ? Math.abs(shellBox.width - emptyBox.width) : missingElementGap,
      emptyCenterGap: emptyBox ? Math.abs(center(shellBox) - center(emptyBox)) : missingElementGap,
      placeholderCenterGap: placeholderBox ? Math.abs(center(shellBox) - center(placeholderBox)) : missingElementGap,
    };
  });
  expect(layout.emptyWidthGap).toBeLessThanOrEqual(1);
  expect(layout.emptyCenterGap).toBeLessThanOrEqual(1);
  expect(layout.placeholderCenterGap).toBeLessThanOrEqual(1);
}

async function expectSideChatFillsPanel(shell: Locator): Promise<void> {
  await expect(shell.locator(".right-panel-side-chat")).toBeVisible();
  const readLayout = () => shell.evaluate((element) => {
    const shellBox = element.getBoundingClientRect();
    const boxFor = (selector: string) => element.querySelector<HTMLElement>(selector)?.getBoundingClientRect();
    const bottomGap = (selector: string) => {
      const box = boxFor(selector);
      return box ? Math.abs(shellBox.bottom - box.bottom) : Number.MAX_SAFE_INTEGER;
    };
    const composerBox = boxFor("[data-composer-seat]");
    const chatContentBox = boxFor(".chat-content");
    const sideChat = element.querySelector<HTMLElement>(".right-panel-side-chat");
    return {
      bodyHolderBottomGap: bottomGap(".ant-tabs-body-holder"),
      bodyBottomGap: bottomGap(".ant-tabs-body"),
      tabContentBottomGap: bottomGap(".ant-tabs-content"),
      sideChatBottomGap: bottomGap(".right-panel-side-chat"),
      chatPageBottomGap: bottomGap(".right-panel-side-chat .chat-page"),
      composerBottomGap: bottomGap("[data-composer-seat]"),
      chatContentHeight: chatContentBox?.height ?? 0,
      contentBeforeComposer: Boolean(chatContentBox && composerBox && chatContentBox.bottom <= composerBox.top + 1),
      horizontalOverflow: sideChat ? sideChat.scrollWidth - sideChat.clientWidth : -1,
    };
  });

  await expect.poll(async () => (await readLayout()).composerBottomGap).toBeLessThanOrEqual(1);
  const layout = await readLayout();
  expect(layout.bodyHolderBottomGap).toBeLessThanOrEqual(1);
  expect(layout.bodyBottomGap).toBeLessThanOrEqual(1);
  expect(layout.tabContentBottomGap).toBeLessThanOrEqual(1);
  expect(layout.sideChatBottomGap).toBeLessThanOrEqual(1);
  expect(layout.chatPageBottomGap).toBeLessThanOrEqual(1);
  expect(layout.chatContentHeight).toBeGreaterThan(0);
  expect(layout.contentBeforeComposer).toBeTruthy();
  expect(layout.horizontalOverflow).toBeLessThanOrEqual(1);
}

async function dragSplitterBy(page: Page, dragger: Locator, deltaX: number): Promise<void> {
  const box = await dragger.boundingBox();
  expect(box).not.toBeNull();
  const startX = box!.x + box!.width / 2;
  const y = box!.y + box!.height / 2;
  await page.mouse.move(startX, y);
  await page.mouse.down();
  await page.mouse.move(startX + deltaX, y, { steps: 8 });
  await page.mouse.up();
}

test("side chat hides its anchor history, survives refresh, and leaves choices after its last tab closes", async ({ page }) => {
  const title = "Right Panel Side Chat E2E";
  const { session_id: sessionId } = await createConversation(page, title);

  await page.goto("/app");
  await page.getByRole("button", { name: title, exact: true }).click();
  await sendMessage(page, page, "main history must stay hidden from side chat");
  await expect(page.getByRole("combobox", { name: "运行模式" })).toBeVisible();

  await openRightPanel(page);
  await expect(page.getByRole("menu")).toHaveCount(0);
  const shell = page.locator(".right-panel-shell");
  await expectEmptyHorizontallyCentered(shell);
  const createResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().includes("/api/right-panel/")
      && response.url().endsWith("/side-chats"),
  );
  await page.getByRole("button", { name: /创建侧边聊天/ }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.ok(), `${createResponse.status()} ${await createResponse.text()}`).toBeTruthy();

  const panel = page.locator(".right-panel-side-chat");
  await expect(panel).toBeVisible();
  const mainChat = page.locator(".ant-splitter-panel").first().locator(".chat-page");
  await expect(mainChat.getByRole("button", { name: /运行模式：Agent/ })).toBeVisible();
  await expect(panel.getByRole("button", { name: /运行模式：Agent/ })).toBeVisible();
  await expect(panel.getByRole("button", { name: /权限模式：只读/ })).toBeVisible();
  await expect(panel.getByRole("button", { name: /思考等级：中/ })).toBeVisible();
  await expectSideChatFillsPanel(shell);
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
  expect(deleteResponse.status()).toBe(204);
  await expect(page.getByText("Unexpected end of JSON input")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /创建侧边聊天/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开终端/ })).toBeVisible();
  await expect(page.locator(".right-panel-shell")).toBeVisible();
  await expectEmptyHorizontallyCentered(shell);

  await page.reload();
  await expect(page.getByRole("button", { name: /创建侧边聊天/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开终端/ })).toBeVisible();
  await expect(page.locator(".right-panel-shell")).toBeVisible();
});

test("desktop resize keeps a 280px side chat usable, collapses below it, and restores that width", async ({ page }) => {
  const title = "Right Panel Resize E2E";
  await createConversation(page, title);

  await page.goto("/app");
  await page.getByRole("button", { name: title, exact: true }).click();
  await sendMessage(page, page, "create a Turn before resizing the panel");
  await openRightPanel(page);

  const shell = page.locator(".right-panel-shell");
  const dragger = page.locator(".ant-splitter-bar").first();
  await expect(shell).toBeVisible();
  const initialWidth = (await shell.boundingBox())?.width ?? 0;
  expect(initialWidth).toBeGreaterThan(400);

  const createResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().includes("/api/right-panel/")
      && response.url().endsWith("/side-chats"),
  );
  await page.getByRole("button", { name: /创建侧边聊天/ }).click();
  expect((await createResponsePromise).ok()).toBeTruthy();

  await dragSplitterBy(page, dragger, initialWidth - 280);
  await expect.poll(async () => (await shell.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(279);
  await expect.poll(async () => (await shell.boundingBox())?.width ?? 0).toBeLessThanOrEqual(281);
  await expectSideChatFillsPanel(shell);

  await dragSplitterBy(page, dragger, 40);

  await expect(page.getByRole("button", { name: "打开右侧边栏" })).toBeVisible();
  await openRightPanel(page);
  await expect(shell).toBeVisible();
  await expect.poll(async () => (await shell.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(279);
  await expect.poll(async () => (await shell.boundingBox())?.width ?? 0).toBeLessThanOrEqual(281);
  await expectSideChatFillsPanel(shell);
});

test("mobile right panel uses a full-width Drawer and keeps the empty creation state", async ({ page }) => {
  await page.setViewportSize({ width: 480, height: 800 });
  const title = "Right Panel Mobile E2E";
  await createConversation(page, title);

  await page.goto("/app");
  await page.getByRole("button", { name: "打开会话列表" }).click();
  await page.getByRole("button", { name: title, exact: true }).click();
  await sendMessage(page, page, "create a Turn before opening the mobile panel");

  await openRightPanel(page);

  const drawer = page.locator(".ant-drawer-content-wrapper:visible");
  await expect(drawer).toBeVisible();
  await expect(page.getByRole("dialog", { name: "右侧边栏" })).toBeVisible();
  await expect(page.getByRole("button", { name: /创建侧边聊天/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开终端/ })).toBeVisible();
  await expect.poll(async () => (await drawer.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(479);
  const drawerShell = drawer.locator(".right-panel-shell");
  await expectEmptyHorizontallyCentered(drawerShell);

  const createResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().includes("/api/right-panel/")
      && response.url().endsWith("/side-chats"),
  );
  await page.getByRole("button", { name: /创建侧边聊天/ }).click();
  expect((await createResponsePromise).ok()).toBeTruthy();
  await expectSideChatFillsPanel(drawerShell);

  await drawer.locator(".ant-drawer-close").click();
  await expect(drawer).toHaveCount(0);
  await expect(page.getByRole("button", { name: "打开右侧边栏" })).toBeVisible();
});

test("real cmd terminal starts in the Turn cwd, replays after refresh, and closes from its tab", async ({ page }) => {
  test.skip(process.platform !== "win32", "Exercises the Windows cmd terminal.");
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

  await openRightPanel(page);
  const createResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().includes("/api/right-panel/")
      && response.url().endsWith("/terminals"),
  );
  await page.getByRole("button", { name: /打开终端/ }).click();
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
  expect(deleteResponse.status()).toBe(204);
  await expect(page.getByRole("button", { name: /创建侧边聊天/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开终端/ })).toBeVisible();

  const panelResponse = await page.request.get(`/api/right-panel/${encodeURIComponent(sessionId)}`);
  expect(panelResponse.ok(), `${panelResponse.status()} ${await panelResponse.text()}`).toBeTruthy();
  expect((await panelResponse.json() as { windows: unknown[] }).windows).toHaveLength(0);
});
