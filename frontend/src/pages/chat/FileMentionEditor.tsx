import {
  $getNodeByKey,
  $getAdjacentNode,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isRangeSelection,
  $nodesOfType,
  $createParagraphNode,
  $createTextNode,
  COMMAND_PRIORITY_HIGH,
  KEY_BACKSPACE_COMMAND,
  KEY_DELETE_COMMAND,
  COPY_COMMAND,
  DecoratorNode,
  type EditorConfig,
  type EditorState,
  type ElementNode,
  type DOMExportOutput,
  type LexicalEditor,
  type LexicalNode,
  type NodeKey,
  type SerializedLexicalNode,
  type TextNode,
} from "lexical";
import { LexicalComposer } from "@lexical/react/LexicalComposer";
import { ContentEditable } from "@lexical/react/LexicalContentEditable";
import { HistoryPlugin } from "@lexical/react/LexicalHistoryPlugin";
import { LexicalErrorBoundary } from "@lexical/react/LexicalErrorBoundary";
import { OnChangePlugin } from "@lexical/react/LexicalOnChangePlugin";
import { PlainTextPlugin } from "@lexical/react/LexicalPlainTextPlugin";
import { useLexicalComposerContext } from "@lexical/react/LexicalComposerContext";
import { useEffect, useImperativeHandle, useRef, forwardRef, type KeyboardEvent, type MutableRefObject, type MouseEvent as ReactMouseEvent, type ReactElement } from "react";
import type { FileReference, FileSource } from "../../types";
import { completionToken, fileTrigger, type FileTrigger } from "../../commands/fileCompletion";

const FILE_MENTION_VERSION = 2;

export interface FileMentionChange {
  prompt: string;
  references: FileReference[];
  caret: number;
  trigger: FileTrigger | null;
  canSend: boolean;
}

export interface FileMentionEditorHandle {
  focus: () => void;
  clear: () => void;
  insertText: (text: string) => void;
  replaceCurrentMention: (reference: FileReference) => void;
  restore: (prompt: string, references?: FileReference[]) => void;
}

interface FileMentionEditorProps {
  disabled?: boolean;
  placeholder?: string;
  onChange: (change: FileMentionChange) => void;
  onPasteFiles?: (event: globalThis.ClipboardEvent) => void;
}

interface SerializedFileMentionNode extends SerializedLexicalNode {
  type: "file-mention";
  version: typeof FILE_MENTION_VERSION;
  source: FileSource;
  path: string;
  displayPath: string;
}

function mentionToken(displayPath: string): string {
  return completionToken(displayPath);
}

export class FileMentionNode extends DecoratorNode<ReactElement> {
  __source: FileSource;
  __path: string;
  __displayPath: string;

  static getType(): string {
    return "file-mention";
  }

  static clone(node: FileMentionNode): FileMentionNode {
    return new FileMentionNode(node.__source, node.__path, node.__displayPath, node.__key);
  }

  static importJSON(serialized: SerializedLexicalNode & Record<string, unknown>): FileMentionNode {
    const data = serialized as Partial<SerializedFileMentionNode>;
    return new FileMentionNode(
      data.source === "upload" || data.source === "workspace" ? data.source : "project",
      typeof data.path === "string" ? data.path : "",
      typeof data.displayPath === "string" ? data.displayPath : "",
    );
  }

  constructor(source: FileSource, path: string, displayPath: string, key?: NodeKey) {
    super(key);
    this.__source = source;
    this.__path = path;
    this.__displayPath = displayPath;
  }

  exportJSON(): SerializedFileMentionNode {
    return {
      type: "file-mention",
      version: FILE_MENTION_VERSION,
      source: this.__source,
      path: this.__path,
      displayPath: this.__displayPath,
    };
  }

  getTextContent(): string {
    return mentionToken(this.__displayPath);
  }

  exportDOM(): DOMExportOutput {
    const element = document.createElement("span");
    element.textContent = mentionToken(this.__displayPath);
    return { element };
  }

  createDOM(_config: EditorConfig): HTMLElement {
    const element = document.createElement("span");
    element.className = "file-mention-node";
    element.setAttribute("data-file-mention", "true");
    element.setAttribute("data-source", this.__source);
    element.setAttribute("contenteditable", "false");
    return element;
  }

  updateDOM(): boolean {
    return false;
  }

  isInline(): boolean {
    return true;
  }

  isIsolated(): boolean {
    return true;
  }

  isKeyboardSelectable(): boolean {
    return true;
  }

  decorate(editor: LexicalEditor): ReactElement {
    return (
      <FileMentionView
        editor={editor}
        nodeKey={this.__key}
        source={this.__source}
        displayPath={this.__displayPath}
      />
    );
  }
}

function $createFileMentionNode(reference: FileReference): FileMentionNode {
  return new FileMentionNode(reference.source, reference.path, reference.display_path);
}

function $isFileMentionNode(node: LexicalNode | null | undefined): node is FileMentionNode {
  return node instanceof FileMentionNode;
}

function FileMentionView({ editor, nodeKey, source, displayPath }: {
  editor: LexicalEditor;
  nodeKey: NodeKey;
  source: FileSource;
  displayPath: string;
}) {
  const remove = (event: ReactMouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    editor.update(() => {
      $getNodeByKey(nodeKey)?.remove();
    });
    editor.focus();
  };
  return (
    <span className={`file-mention-bubble ${source}`} contentEditable={false} data-file-mention-bubble="true" title={displayPath}>
      <span className="file-mention-label">{displayPath}</span>
      <button type="button" className="file-mention-remove" aria-label={`移除引用 ${displayPath}`} onMouseDown={remove}>×</button>
    </span>
  );
}

function findTextPoint(root: ReturnType<typeof $getRoot>, offset: number): { node: TextNode; offset: number } | null {
  let cursor = 0;
  const texts = root.getAllTextNodes();
  for (const text of texts) {
    const end = cursor + text.getTextContentSize();
    if (offset <= end) return { node: text, offset: Math.max(0, offset - cursor) };
    cursor = end;
  }
  const last = texts[texts.length - 1];
  return last ? { node: last, offset: last.getTextContentSize() } : null;
}

function childTextContribution(children: LexicalNode[], index: number): number {
  const child = children[index];
  if (!child) return 0;
  const blockSeparator = $isElementNode(child) && !child.isInline() && index < children.length - 1 ? 2 : 0;
  return child.getTextContentSize() + blockSeparator;
}

function textOffsetBeforeNode(parent: ElementNode, target: LexicalNode): number | null {
  if (parent.is(target)) return 0;
  const children = parent.getChildren();
  let offset = 0;
  for (let index = 0; index < children.length; index += 1) {
    const child = children[index]!;
    if (child.is(target)) return offset;
    if ($isElementNode(child)) {
      const nestedOffset = textOffsetBeforeNode(child, target);
      if (nestedOffset !== null) return offset + nestedOffset;
    }
    offset += childTextContribution(children, index);
  }
  return null;
}

function currentCaret(root: ReturnType<typeof $getRoot>): number {
  const selection = $getSelection();
  if (!$isRangeSelection(selection)) return root.getTextContentSize();
  const anchor = selection.anchor;
  const anchorNode = anchor.getNode();
  const nodeOffset = textOffsetBeforeNode(root, anchorNode);
  if (nodeOffset === null) return root.getTextContentSize();
  if (anchor.type === "text") return nodeOffset + anchor.offset;
  if ($isElementNode(anchorNode)) {
    const children = anchorNode.getChildren();
    let childOffset = 0;
    for (let index = 0; index < Math.min(anchor.offset, children.length); index += 1) {
      childOffset += childTextContribution(children, index);
    }
    return nodeOffset + childOffset;
  }
  return nodeOffset;
}

function buildPromptNodes(prompt: string, references: FileReference[]): LexicalNode[] {
  const matches: Array<{ start: number; end: number; reference: FileReference }> = [];
  const used = new Map<string, number>();
  for (const reference of references) {
    const token = mentionToken(reference.display_path);
    const key = `${reference.source}:${token}`;
    const from = used.get(key) ?? 0;
    const index = prompt.indexOf(token, from);
    if (index >= 0) {
      matches.push({ start: index, end: index + token.length, reference });
      used.set(key, index + token.length);
    }
  }
  matches.sort((a, b) => a.start - b.start || a.end - b.end);
  const nodes: LexicalNode[] = [];
  let cursor = 0;
  for (const match of matches) {
    if (match.start < cursor) continue;
    if (match.start > cursor) nodes.push($createTextNode(prompt.slice(cursor, match.start)));
    nodes.push($createFileMentionNode(match.reference));
    cursor = match.end;
  }
  if (cursor < prompt.length) nodes.push($createTextNode(prompt.slice(cursor)));
  if (nodes.length === 0) nodes.push($createTextNode(""));
  return nodes;
}

function EditorBridge({ handleRef, disabled, placeholder, onChange, onPasteFiles }: FileMentionEditorProps & { handleRef: MutableRefObject<FileMentionEditorHandle | null> }) {
  const [editor] = useLexicalComposerContext();
  const contentEditableRef = useRef<HTMLDivElement | null>(null);
  const changeRef = useRef<FileMentionChange | null>(null);

  useImperativeHandle(handleRef, () => ({
    focus: () => editor.focus(),
    clear: () => editor.update(() => {
      const root = $getRoot();
      root.clear();
      root.append($createParagraphNode());
      root.selectEnd();
    }),
    insertText: (text) => editor.update(() => {
      const root = $getRoot();
      const selection = $getSelection();
      if ($isRangeSelection(selection)) selection.insertNodes([$createTextNode(text)]);
      else root.selectEnd().insertNodes([$createTextNode(text)]);
    }),
    replaceCurrentMention: (reference) => editor.update(() => {
      const change = changeRef.current;
      const trigger = change?.trigger;
      const root = $getRoot();
      if (!change || !trigger) return;
      const start = findTextPoint(root, trigger.start);
      const end = findTextPoint(root, trigger.end);
      if (!start || !end) return;
      const selection = $getSelection();
      if (!$isRangeSelection(selection)) return;
      selection.setTextNodeRange(start.node, start.offset, end.node, end.offset);
      selection.removeText();
      const mention = $createFileMentionNode(reference);
      const followingCharacter = change.prompt.slice(trigger.end, trigger.end + 1);
      const spacer = /\s/.test(followingCharacter) ? null : $createTextNode(" ");
      selection.insertNodes(spacer ? [mention, spacer] : [mention]);
      spacer?.selectEnd();
    }),
    restore: (prompt, references = []) => editor.update(() => {
      const root = $getRoot();
      root.clear();
      const paragraph = $createParagraphNode();
      paragraph.append(...buildPromptNodes(prompt, references));
      root.append(paragraph);
      root.selectEnd();
    }),
  }), [editor, handleRef]);

  useEffect(() => {
    const element = contentEditableRef.current;
    if (!element) return;
    editor.setEditable(!disabled);
    element.setAttribute("placeholder", placeholder ?? "");
    Object.defineProperty(element, "value", { configurable: true, get: () => changeRef.current?.prompt ?? "" });
  }, [disabled, editor, placeholder]);

  function emitChange(editorState: EditorState) {
    editorState.read(() => {
      const root = $getRoot();
      const prompt = root.getTextContent();
      const selection = $getSelection();
      const caret = currentCaret(root);
      const references = $nodesOfType(FileMentionNode).map((node) => ({
        source: node.__source,
        path: node.__path,
        display_path: node.__displayPath,
      }));
      const next = { prompt, references, caret, trigger: fileTrigger(prompt, caret), canSend: Boolean(prompt.trim() || references.length) };
      changeRef.current = next;
      onChange(next);
      void selection;
    });
  }

  useEffect(() => {
    const root = contentEditableRef.current;
    if (!root) return;
    const handler = (event: ClipboardEvent) => onPasteFiles?.(event);
    root.addEventListener("paste", handler);
    return () => root.removeEventListener("paste", handler);
  }, [onPasteFiles]);

  useEffect(() => {
    function deleteMentionAtCaret(): boolean {
      let removed = false;
      editor.update(() => {
        const selection = $getSelection();
        if (!$isRangeSelection(selection) || !selection.isCollapsed()) return;
        const adjacent = $getAdjacentNode(selection.anchor, true);
        if (!$isFileMentionNode(adjacent)) return;
        adjacent.selectPrevious();
        adjacent.remove();
        removed = true;
      });
      return removed;
    }
    const backspace = editor.registerCommand(KEY_BACKSPACE_COMMAND, () => deleteMentionAtCaret(), COMMAND_PRIORITY_HIGH);
    const del = editor.registerCommand(KEY_DELETE_COMMAND, () => {
      let removed = false;
      editor.update(() => {
        const selection = $getSelection();
        if (!$isRangeSelection(selection) || !selection.isCollapsed()) return;
        const adjacent = $getAdjacentNode(selection.anchor, false);
        if (!$isFileMentionNode(adjacent)) return;
        adjacent.selectNext();
        adjacent.remove();
        removed = true;
      });
      return removed;
    }, COMMAND_PRIORITY_HIGH);
    const copy = editor.registerCommand(COPY_COMMAND, (event) => {
      const text = editor.getEditorState().read(() => {
        const selection = $getSelection();
        return $isRangeSelection(selection) && !selection.isCollapsed() ? selection.getTextContent() : $getRoot().getTextContent();
      });
      if (event && "clipboardData" in event) event.clipboardData?.setData("text/plain", text);
      event?.preventDefault();
      return true;
    }, COMMAND_PRIORITY_HIGH);
    return () => { backspace(); del(); copy(); };
  }, [editor]);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.defaultPrevented || event.nativeEvent.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    // jsdom does not implement the browser beforeinput editing pipeline.
    // Keep unit tests deterministic without changing the real browser path.
    if (typeof navigator === "undefined" || !/jsdom/i.test(navigator.userAgent) || event.nativeEvent.isTrusted) return;
    if (event.key.length === 1) {
      event.preventDefault();
      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) selection.insertText(event.key);
      });
      emitChange(editor.getEditorState());
    }
  }

  return (
    <>
      <PlainTextPlugin
        contentEditable={<ContentEditable ref={contentEditableRef} className="composer-input file-mention-editor" data-reveal-index="3" aria-label="聊天输入" placeholder={null} onKeyDown={handleKeyDown} />}
        placeholder={null}
        ErrorBoundary={LexicalErrorBoundary}
      />
      <OnChangePlugin onChange={emitChange} />
      <HistoryPlugin />
    </>
  );
}

const FileMentionEditor = forwardRef<FileMentionEditorHandle, FileMentionEditorProps>(function FileMentionEditor(props, ref) {
  const handleRef = useRef<FileMentionEditorHandle | null>(null);
  useImperativeHandle(ref, () => ({
    focus: () => handleRef.current?.focus(),
    clear: () => handleRef.current?.clear(),
    insertText: (text) => handleRef.current?.insertText(text),
    replaceCurrentMention: (reference) => handleRef.current?.replaceCurrentMention(reference),
    restore: (prompt, references) => handleRef.current?.restore(prompt, references),
  }), []);
  const initialConfig = {
    namespace: "mini-agent-file-mentions",
    nodes: [FileMentionNode],
    editable: !props.disabled,
    onError: (error: Error) => { throw error; },
    editorState: (editor: LexicalEditor) => {
      editor.update(() => {
        const root = $getRoot();
        root.clear();
        root.append($createParagraphNode());
      });
    },
  };
  return (
    <LexicalComposer initialConfig={initialConfig}>
      <EditorBridge {...props} handleRef={handleRef} />
    </LexicalComposer>
  );
});

export default FileMentionEditor;
