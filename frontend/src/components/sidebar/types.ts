import type { ProjectInfo, SidebarThreadSort } from "../../api";
import type { Conversation, LocalProfile, Page } from "../../types";

export interface AppSidebarProps {
  profile: LocalProfile;
  conversations: Conversation[];
  archivedCount: number;
  currentId: string | null;
  page: Page;
  onNew: () => void | Promise<unknown>;
  projects?: ProjectInfo[];
  projectsLoaded?: boolean;
  projectLoading?: boolean;
  onNewProject?: () => void | Promise<unknown>;
  onNewProjectConversation?: (projectId: string) => void | Promise<unknown>;
  onRemoveProject?: (projectId: string) => void | Promise<unknown>;
  onRenameProject?: (projectId: string, name: string) => void | Promise<unknown>;
  onChangeProjectPath?: (projectId: string) => void | Promise<unknown>;
  onRevokeSkillTrust?: (projectId: string) => void | Promise<unknown>;
  onSelect: (id: string) => void;
  onNavigate: (page: Page) => void;
  onRename: (id: string, title: string) => Promise<void>;
  onArchive: (id: string) => Promise<void>;
  onDelete: (id: string) => void | Promise<void>;
  onReorder?: (projectId: string | null, orderedThreadIds: string[]) => Promise<void>;
  onSort?: (projectId: string | null, sortBy: SidebarThreadSort) => Promise<void>;
  onProfileUpdate?: (profile: { display_name: string; agent_preferences: string }) => Promise<void>;
  onOpenSettings?: () => void;
  collapsed?: boolean;
  onToggleCollapse?: () => void;
  revealKey?: number;
}

export interface HistoryMutationProps {
  onRename: (id: string, title: string) => Promise<void>;
  onArchive: (id: string) => Promise<void>;
  onDelete: (id: string) => void | Promise<void>;
}
