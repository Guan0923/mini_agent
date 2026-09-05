import { expect, test } from "@playwright/test";

for (const projectMode of [false, true]) {
  test(`scoped paths use the correct files in ${projectMode ? "project" : "ordinary"} conversations`, async ({ page }) => {
    test.setTimeout(120_000);
    const title = projectMode ? "Scoped Project" : "Scoped Workspace";
    let endpoint = "/api/sidebar-threads";
    if (projectMode) {
      const project = await (await page.request.post("/api/test/project")).json();
      endpoint = `/api/projects/${project.project_id}/sessions`;
    }
    const response = await page.request.post(endpoint, { data: { title } });
    expect(response.ok(), await response.text()).toBeTruthy();
    const payload = await response.json();
    const sidebar = (projectMode ? payload.session : payload) as { session_id: string };
    for (const source of projectMode ? ["workspace", "project"] : ["workspace"]) {
      const file = await page.request.post("/api/test/session-file", { data: {
        session_id: sidebar.session_id, source, display_path: "same.txt", content: `${source} contents`,
      } });
      expect(file.ok(), await file.text()).toBeTruthy();
    }
    await page.request.post("/api/test/session-file", { data: {
      session_id: sidebar.session_id, source: "workspace", display_path: "generated/only.txt", content: "workspace only",
    } });
    await page.goto("/app");
    if (projectMode) {
      const group = page.locator(".ant-collapse-header").filter({ hasText: payload.project.name });
      await expect(group).toBeVisible();
      if (await group.getAttribute("aria-expanded") !== "true") await group.click();
    }
    const thread = page.getByRole("button", { name: title, exact: true });
    await expect(thread).toBeVisible();
    if (await thread.isEnabled()) await thread.click();
    const editor = page.getByLabel("聊天输入");
    await editor.fill("@same");
    await expect(page.locator(".file-item")).toHaveCount(projectMode ? 2 : 1);
    await expect(page.locator(".file-item").filter({ hasText: "workspace:same.txt" })).toBeVisible();
    if (projectMode) await expect(page.locator(".file-item").filter({ hasText: "project:same.txt" })).toBeVisible();
    await editor.fill("@workspace:same");
    await expect(page.locator(".file-item")).toHaveCount(1);
    await editor.press("Enter");
    await expect(page.locator(".file-mention-label")).toHaveText("workspace:same.txt");
    expect(await editor.evaluate((element) => (element as HTMLDivElement & { value: string }).value)).toBe("@workspace:same.txt ");
    await page.getByRole("button", { name: "移除引用 workspace:same.txt" }).click();
    await expect(page.locator(".file-mention-bubble")).toHaveCount(0);

    await editor.fill("@workspace:generated/");
    await expect(page.locator(".file-item")).toContainText("workspace:generated/only.txt");
    await editor.press("Tab");
    const sent = page.waitForResponse((item) => item.request().method() === "POST" && item.url().endsWith("/api/turns"));
    await page.getByRole("button", { name: "发送", exact: true }).click();
    expect((await sent).ok()).toBeTruthy();
    await expect(page.locator(".message-reference-path").last()).toHaveText("workspace:generated/only.txt");
    await expect(page.locator(".message.assistant").last().getByRole("button", { name: "Fork" })).toBeVisible({ timeout: 15_000 });
    await page.reload();
    await expect(page.locator(".message-reference-path").last()).toHaveText("workspace:generated/only.txt");

    await page.locator('input[type="file"]').setInputFiles({ name: "upload note.txt", mimeType: "text/plain", buffer: Buffer.from("uploaded") });
    await expect(page.locator(".composer-upload-name")).toHaveText("workspace:uploads/upload note.txt");
    await page.getByRole("button", { name: "移除 upload note.txt", exact: true }).click();
    await expect(page.locator(".composer-upload-name")).toHaveCount(0);

    for (const [path, expected] of [
      ["same.txt", `${projectMode ? "project" : "workspace"} contents`],
      ["workspace:same.txt", "workspace contents"],
      ...(projectMode ? [["project:same.txt", "project contents"], ["generated/only.txt", "Not a file: project:generated/only.txt"]] : []),
    ]) {
      const previousCount = await page.locator(".message.assistant").count();
      await editor.fill(`scoped-read-e2e ${path}`);
      const accepted = page.waitForResponse((item) => item.request().method() === "POST" && item.url().endsWith("/api/turns"));
      await page.getByRole("button", { name: "发送", exact: true }).click();
      expect((await accepted).ok()).toBeTruthy();
      await expect(page.locator(".message.assistant")).toHaveCount(previousCount + 1);
      await expect(page.locator(".message.assistant").last().getByRole("button", { name: "Fork" })).toBeVisible({ timeout: 20_000 });
      await expect(page.locator(".message.assistant").last()).toContainText(expected);
    }

    const longRelative = `generated/reports/${"review-notes-".repeat(4)}report.txt`;
    const longFile = await page.request.post("/api/test/session-file", { data: {
      session_id: sidebar.session_id, source: "workspace", display_path: longRelative, content: "long path",
    } });
    expect(longFile.ok(), await longFile.text()).toBeTruthy();
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await editor.fill("@workspace:generated/reports/");
      await expect(page.locator(".file-item")).toBeVisible();
      await expect(page.locator(".file-item-path")).toHaveAttribute("title", `workspace:${longRelative}`);
      await editor.press("Tab");
      const bubble = page.locator(".file-mention-bubble");
      await expect(bubble).toContainText(`workspace:${longRelative}`);
      const box = await bubble.boundingBox();
      expect(box && box.x >= 0 && box.x + box.width <= width).toBeTruthy();
      await page.screenshot({ path: `test-results/scoped-${projectMode ? "project" : "workspace"}-${width}.png` });
    }
  });
}
