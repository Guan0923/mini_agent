import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import App from "./App";

beforeEach(() => localStorage.clear());

describe("conversation recovery", () => {
  it("does not restore a stale running animation from localStorage", () => {
    localStorage.setItem(
      "mini-agent-conversations",
      JSON.stringify([
        {
          id: "old-run",
          title: "旧任务",
          messages: [
            { id: "assistant-1", role: "assistant", content: "", events: [], running: true },
          ],
        },
      ]),
    );

    render(<App />);

    expect(screen.getByText("上次运行已中断")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "思考中" })).not.toBeInTheDocument();
  });
});
