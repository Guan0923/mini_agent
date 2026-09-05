export type FileSource = "project" | "upload" | "workspace";

export const fileSourceLabels: Record<FileSource, string> = {
  project: "项目文件",
  upload: "会话上传",
  workspace: "会话文件",
};

export interface FileReference {
  source: FileSource;
  /** Canonical workspace: or project: path returned by the backend. */
  path: string;
  /** Full prefixed relative path used for every user-visible label. */
  display_path: string;
}

export interface SessionFileInfo {
  source: FileSource;
  /** Canonical workspace: or project: path returned by the backend. */
  path: string;
  /** Full prefixed relative path used for every user-visible label. */
  display_path: string;
  name: string;
  size: number;
  mime: string;
  mtime: string;
  is_image: boolean;
}
