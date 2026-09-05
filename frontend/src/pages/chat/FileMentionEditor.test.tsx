import { act, render, screen, waitFor } from "@testing-library/react";
import { useRef, useState } from "react";
import { describe, expect, it } from "vitest";
import type { FileReference } from "../../types";
import FileMentionEditor, { type FileMentionChange, type FileMentionEditorHandle } from "./FileMentionEditor";

function Harness({
  initial,
  references = [],
  completion,
}: {
  initial: string;
  references?: FileReference[];
  completion?: FileReference;
}) {
  const editorRef = useRef<FileMentionEditorHandle>(null);
  const [change, setChange] = useState<FileMentionChange | null>(null);
  return (
    <>
      <FileMentionEditor ref={editorRef} onChange={setChange} />
      <button type="button" onClick={() => editorRef.current?.restore(initial, references)}>恢复</button>
      {completion ? <button type="button" onClick={() => editorRef.current?.replaceCurrentMention(completion)}>补全</button> : null}
      <output data-testid="prompt">{change?.prompt ?? ""}</output>
      <output data-testid="references">{JSON.stringify(change?.references ?? [])}</output>
      <output data-testid="trigger">{JSON.stringify(change?.trigger ?? null)}</output>
    </>
  );
}

describe("FileMentionEditor", () => {
  it.each(["workspace", "project", "upload"] as const)("preserves prefixed %s references through restore", async (source) => {
    const path = source === "upload" ? "workspace:uploads/my notes.txt" : `${source}:docs/my notes.txt`;
    const reference = { source, path, display_path: path };
    render(<Harness initial={`查看 @"${path}"`} references={[reference]} />);
    await act(async () => { screen.getByRole("button", { name: "恢复" }).click(); });
    expect(await screen.findByText(path, { selector: ".file-mention-label" })).toBeInTheDocument();
    expect(screen.getByTestId("references")).toHaveTextContent(JSON.stringify([reference]));
    expect(screen.getByTestId("prompt")).toHaveTextContent(`查看 @"${path}"`);
    expect(document.querySelector('[data-file-mention="true"]')).toHaveAttribute("data-source", source);
    await act(async () => { screen.getByRole("button", { name: `移除引用 ${path}` }).dispatchEvent(new MouseEvent("mousedown", { bubbles: true })); });
    expect(screen.getByTestId("references")).toHaveTextContent("[]");
  });
  it("restores referenced tokens as atomic bubbles and serializes their plain token", async () => {
    const reference = {
      source: "project" as const,
      path: "C:/workspace/my notes.txt",
      display_path: "my notes.txt",
    };
    render(<Harness initial={'请查看 @"my notes.txt"'} references={[reference]} />);
    await act(async () => { screen.getByRole("button", { name: "恢复" }).click(); });

    expect(await screen.findByText("my notes.txt", { selector: ".file-mention-label" })).toBeInTheDocument();
    expect(screen.getByTestId("prompt")).toHaveTextContent('请查看 @"my notes.txt"');
    expect(screen.getByTestId("references")).toHaveTextContent(JSON.stringify([reference]));
    expect(screen.getByLabelText("聊天输入")).not.toHaveTextContent(reference.path);
    expect(document.querySelector(`[title="${reference.path}"]`)).toBeNull();
    expect(screen.queryByLabelText(`移除引用 ${reference.path}`)).not.toBeInTheDocument();
  });

  it("removes only the selected duplicate bubble", async () => {
    const reference = { source: "upload" as const, path: "C:/uploads/a.txt", display_path: "a.txt" };
    render(<Harness initial="@a.txt @a.txt" references={[reference, reference]} />);
    await act(async () => { screen.getByRole("button", { name: "恢复" }).click(); });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "移除引用 a.txt" })).toHaveLength(2));

    await act(async () => { screen.getAllByRole("button", { name: "移除引用 a.txt" })[0]?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })); });
    expect(screen.getAllByRole("button", { name: "移除引用 a.txt" })).toHaveLength(1);
    expect(screen.getByTestId("references")).toHaveTextContent(JSON.stringify([reference]));
  });

  it("adds one editable space after a completed mention", async () => {
    const reference = {
      source: "project" as const,
      path: "C:/workspace/docs/alpha-note.txt",
      display_path: "docs/alpha-note.txt",
    };
    render(<Harness initial="before @alpha" completion={reference} />);
    await act(async () => { screen.getByRole("button", { name: "恢复" }).click(); });
    await waitFor(() => expect(screen.getByTestId("prompt").textContent).toBe("before @alpha"));

    await act(async () => { screen.getByRole("button", { name: "补全" }).click(); });

    await waitFor(() => expect(screen.getByTestId("prompt").textContent).toBe("before @docs/alpha-note.txt "));
    expect(screen.getByTestId("references")).toHaveTextContent(JSON.stringify([reference]));
    expect(screen.getByTestId("trigger").textContent).toBe("null");
    const editor = screen.getByLabelText("聊天输入");
    expect(editor.querySelectorAll("p")).toHaveLength(1);
    expect(editor.querySelector('br:not([data-lexical-managed-linebreak="true"])')).toBeNull();
    expect(editor.querySelector("p")?.lastChild?.textContent).toBe(" ");
  });
});
