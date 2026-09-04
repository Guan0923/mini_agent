export type FileSource = "project" | "upload";

export interface FileReference {
  source: FileSource;
  /** Canonical absolute local path; never render this value in the UI. */
  path: string;
  /** Source-root-relative path used for every user-visible label. */
  display_path: string;
}

export interface SessionFileInfo {
  source: FileSource;
  /** Canonical absolute local path; never render this value in the UI. */
  path: string;
  /** Source-root-relative path used for every user-visible label. */
  display_path: string;
  name: string;
  size: number;
  mime: string;
  mtime: string;
  is_image: boolean;
}
