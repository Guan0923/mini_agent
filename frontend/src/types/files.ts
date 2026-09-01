export type FileSource = "project" | "upload";

export interface FileReference {
  source: FileSource;
  path: string;
}

export interface SessionFileInfo {
  source: FileSource;
  path: string;
  name: string;
  size: number;
  mime: string;
  mtime: string;
  is_image: boolean;
}
