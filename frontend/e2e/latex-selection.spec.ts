import { expect, test } from "@playwright/test";

async function readClipboard(page: import("@playwright/test").Page): Promise<string> {
  return page.evaluate(() => navigator.clipboard.readText());
}

test.describe("native MathML formula selection", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/e2e/markdown-fixture.html");
    await expect(page.locator('[data-math-renderer="mathml"]')).toHaveCount(2);
  });

  test("copies the complete source after selecting part of a formula with the mouse", async ({ page }) => {
    const formula = page.locator('[data-latex-source="$x^2$"]');
    const identifier = formula.locator("mi");
    const box = await identifier.boundingBox();
    expect(box).not.toBeNull();

    await page.mouse.move(box!.x + 1, box!.y + box!.height / 2);
    await page.mouse.down();
    await page.mouse.move(box!.x + Math.max(1, box!.width - 1), box!.y + box!.height / 2);
    await page.mouse.up();

    const selection = await page.evaluate(() => {
      const current = window.getSelection();
      return {
        collapsed: current?.isCollapsed ?? true,
        anchorInsideMath: current?.anchorNode?.parentElement?.closest("math") !== null,
        focusInsideMath: current?.focusNode?.parentElement?.closest("math") !== null,
      };
    });
    expect(selection.collapsed).toBe(false);
    expect(selection.anchorInsideMath || selection.focusInsideMath).toBe(true);

    await page.keyboard.press("Control+C");
    await expect.poll(() => readClipboard(page)).toBe("$x^2$");
  });

  test("keeps selected neighbor text and completes a partially selected formula", async ({ page }) => {
    await page.evaluate(() => {
      const root = document.querySelector(".markdown")!;
      const before = root.querySelector("p")!.firstChild!;
      const formulaText = root.querySelector('[data-latex-source="$x^2$"] mi')!.firstChild!;
      const range = document.createRange();
      range.setStart(before, 1);
      range.setEnd(formulaText, 1);
      const selection = window.getSelection()!;
      selection.removeAllRanges();
      selection.addRange(range);
    });

    await page.keyboard.press("Control+C");
    await expect.poll(() => readClipboard(page)).toBe("文 $x^2$");
  });
});
