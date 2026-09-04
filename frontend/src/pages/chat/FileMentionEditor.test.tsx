import { act, render, screen, waitFor } from "@testing-library/react";
import { useRef, useState } from "react";
import { describe, expect, it } from "vitest";
import type { FileReference } from "../../types";
import FileMentionEditor, { type FileMentionChange, type FileMentionEditorHandle } from "./FileMentionEditor";

function Harness({ initial, references = [] }: { initial: string; references?: FileReference[] }) {
  const editorRef = useRef<FileMentionEditorHandle>(null);
  const [change, setChange] = useState<FileMentionChange | null>(null);
  return (
    <>
      <FileMentionEditor ref={editorRef} onChange={setChange} />
      <button type="button" onClick={() => editorRef.current?.restore(initial, references)}>恢复</button>
      <output data-testid="prompt">{change?.prompt ?? ""}</output>
      <output data-testid="references">{JSON.stringify(change?.references ?? [])}</output>
    </>
  );
}

describe("FileMentionEditor", () => {
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
});
