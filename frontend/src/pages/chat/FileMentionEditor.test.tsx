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
    render(<Harness initial={'请查看 @"my notes.txt"'} references={[{ source: "project", path: "my notes.txt" }]} />);
    await act(async () => { screen.getByRole("button", { name: "恢复" }).click(); });

    expect(await screen.findByText("my notes.txt", { selector: ".file-mention-label" })).toBeInTheDocument();
    expect(screen.getByTestId("prompt")).toHaveTextContent('请查看 @"my notes.txt"');
    expect(screen.getByTestId("references")).toHaveTextContent(JSON.stringify([{ source: "project", path: "my notes.txt" }]));
  });

  it("removes only the selected duplicate bubble", async () => {
    render(<Harness initial="@a.txt @a.txt" references={[{ source: "upload", path: "a.txt" }, { source: "upload", path: "a.txt" }]} />);
    await act(async () => { screen.getByRole("button", { name: "恢复" }).click(); });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "移除引用 a.txt" })).toHaveLength(2));

    await act(async () => { screen.getAllByRole("button", { name: "移除引用 a.txt" })[0]?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })); });
    expect(screen.getAllByRole("button", { name: "移除引用 a.txt" })).toHaveLength(1);
    expect(screen.getByTestId("references")).toHaveTextContent(JSON.stringify([{ source: "upload", path: "a.txt" }]));
  });
});
