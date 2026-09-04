import { expect, test, type Locator, type Page } from "@playwright/test";

interface SidebarThread {
  session_id: string;
  thread_id: string;
  title: string;
  project_id: string | null;
}

interface SidebarProjectResult {
  project: { project_id: string; name: string };
  threads: SidebarThread[];
}

async function createOrdinaryConversation(page: Page, title: string): Promise<SidebarThread> {
  const createResponse = await page.request.post("/api/sidebar-threads", { data: { title } });
  expect(createResponse.ok(), `${createResponse.status()} ${await createResponse.text()}`).toBeTruthy();
  const created = await createResponse.json() as SidebarThread;
  const renameResponse = await page.request.patch(`/api/sidebar-threads/${encodeURIComponent(created.thread_id)}`, {
    data: { title },
  });
  expect(renameResponse.ok(), `${renameResponse.status()} ${await renameResponse.text()}`).toBeTruthy();
  return { ...created, ...await renameResponse.json() as SidebarThread };
}

async function createProjectConversations(
  page: Page,
  name: string,
  titles: string[],
): Promise<SidebarProjectResult> {
  const response = await page.request.post("/api/test/sidebar-project", { data: { name, titles } });
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  return response.json() as Promise<SidebarProjectResult>;
}

function historyRow(scope: Locator, title: string): Locator {
  return scope.locator(`.history-list-item[aria-label="拖动排序：${title}"]`);
}

async function historyTitles(scope: Locator): Promise<string[]> {
  return scope.locator(".history-entry-button").evaluateAll((buttons) => (
    buttons.map((button) => button.getAttribute("aria-label") ?? "")
  ));
}

async function dragRow(page: Page, source: Locator, target: Locator): Promise<void> {
  const sourceBox = await source.boundingBox();
  expect(sourceBox).not.toBeNull();
  const startX = sourceBox!.x + 16;
  const startY = sourceBox!.y + sourceBox!.height / 2;
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(startX + 12, startY, { steps: 2 });
  await expect(source).toHaveClass(/is-dragging/);
  const targetBox = await target.boundingBox();
  expect(targetBox).not.toBeNull();
  const targetX = targetBox!.x + 16;
  const targetY = targetBox!.y + targetBox!.height / 2;
  await page.mouse.move(targetX, targetY, { steps: 10 });
  await page.mouse.up();
}

async function chooseSort(
  page: Page,
  button: Locator,
  label: "按创建时间" | "按最近聊天",
  projectId: string | null,
): Promise<void> {
  const responsePromise = page.waitForResponse(async (response) => {
    if (response.request().method() !== "PUT" || !response.url().endsWith("/api/sidebar-threads/order")) return false;
    return (response.request().postDataJSON() as { project_id?: string | null }).project_id === projectId;
  });
  await button.click();
  await page.getByRole("menuitem", { name: label, exact: true }).click();
  const response = await responsePromise;
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
}

async function sendMessage(page: Page, text: string): Promise<void> {
  await page.getByLabel("聊天输入").fill(text);
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST" && response.url().endsWith("/api/turns")
  ));
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const response = await responsePromise;
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  const assistant = page.locator(".message.assistant").last().locator(".assistant-run-frame");
  await expect(assistant).toBeVisible({ timeout: 15_000 });
  await expect(assistant).not.toHaveClass(/is-running/, { timeout: 15_000 });
}

test("sidebar groups drag and apply one-time sorts with persisted order", async ({ page }) => {
  test.slow();
  const ordinaryTitles = ["普通排序一", "普通排序二", "普通排序三"];
  const projectTitles = ["项目排序一", "项目排序二", "项目排序三"];
  for (const title of ordinaryTitles) await createOrdinaryConversation(page, title);
  const projectResult = await createProjectConversations(page, "排序验收项目", projectTitles);

  await page.goto("/app");
  const ordinaryScope = page.locator(".sidebar-ordinary-history");
  const projectItem = page.locator(".ant-collapse-item").filter({ hasText: projectResult.project.name });
  if (!await projectItem.evaluate((item) => item.classList.contains("ant-collapse-item-active"))) {
    await projectItem.locator(":scope > .ant-collapse-header").click();
  }
  const projectScope = projectItem;

  await expect.poll(() => historyTitles(ordinaryScope)).toEqual([...ordinaryTitles].reverse());
  await expect.poll(() => historyTitles(projectScope)).toEqual([...projectTitles].reverse());

  const ordinaryDragResponse = page.waitForResponse((response) => (
    response.request().method() === "PUT" && response.url().endsWith("/api/sidebar-threads/order")
  ));
  await dragRow(page, historyRow(ordinaryScope, ordinaryTitles[2]), historyRow(ordinaryScope, ordinaryTitles[0]));
  expect((await ordinaryDragResponse).ok()).toBeTruthy();
  const ordinaryManualOrder = [ordinaryTitles[1], ordinaryTitles[0], ordinaryTitles[2]];
  await expect.poll(() => historyTitles(ordinaryScope)).toEqual(ordinaryManualOrder);

  const projectDragResponse = page.waitForResponse((response) => (
    response.request().method() === "PUT" && response.url().endsWith("/api/sidebar-threads/order")
  ));
  await dragRow(page, historyRow(projectScope, projectTitles[2]), historyRow(projectScope, projectTitles[0]));
  expect((await projectDragResponse).ok()).toBeTruthy();
  const projectManualOrder = [projectTitles[1], projectTitles[0], projectTitles[2]];
  await expect.poll(() => historyTitles(projectScope)).toEqual(projectManualOrder);

  await chooseSort(
    page,
    projectItem.getByRole("button", { name: "对话排序", exact: true }),
    "按创建时间",
    projectResult.project.project_id,
  );
  await expect.poll(() => historyTitles(projectScope)).toEqual([...projectTitles].reverse());

  await page.getByRole("button", { name: ordinaryTitles[0], exact: true }).click();
  await sendMessage(page, "更新普通对话一的活动时间");
  await expect.poll(() => historyTitles(ordinaryScope)).toEqual(ordinaryManualOrder);

  await chooseSort(
    page,
    ordinaryScope.getByRole("button", { name: "对话排序" }),
    "按最近聊天",
    null,
  );
  const ordinaryRecentOrder = [ordinaryTitles[0], ordinaryTitles[2], ordinaryTitles[1]];
  await expect.poll(() => historyTitles(ordinaryScope)).toEqual(ordinaryRecentOrder);

  await page.getByRole("button", { name: ordinaryTitles[1], exact: true }).click();
  await sendMessage(page, "再次聊天但不自动改变固定顺序");
  await expect.poll(() => historyTitles(ordinaryScope)).toEqual(ordinaryRecentOrder);

  await dragRow(page, historyRow(ordinaryScope, ordinaryTitles[0]), historyRow(projectScope, projectTitles[2]));
  await expect.poll(() => historyTitles(ordinaryScope)).toEqual(ordinaryRecentOrder);
  await expect.poll(() => historyTitles(projectScope)).toEqual([...projectTitles].reverse());

  await page.reload();
  const restoredProjectItem = page.locator(".ant-collapse-item").filter({ hasText: projectResult.project.name });
  if (!await restoredProjectItem.evaluate((item) => item.classList.contains("ant-collapse-item-active"))) {
    await restoredProjectItem.locator(":scope > .ant-collapse-header").click();
  }
  await expect.poll(() => historyTitles(page.locator(".sidebar-ordinary-history"))).toEqual(ordinaryRecentOrder);
  await expect.poll(() => historyTitles(restoredProjectItem)).toEqual(
    [...projectTitles].reverse(),
  );
});
