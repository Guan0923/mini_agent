import { useEffect, useRef, useState } from "react";

const PROJECT_STORAGE_KEY = "mini-agent-project-collapse";

export function useProjectExpansion(projectIds: string[], currentProjectId?: string, projectsLoaded = true) {
  const [expandedProjectIds, setExpandedProjectIds] = useState<string[]>([]);
  const loaded = useRef(false);
  const previousCurrentProjectId = useRef<string | undefined>(undefined);

  useEffect(() => {
    try {
      const parsed = JSON.parse(localStorage.getItem(PROJECT_STORAGE_KEY) ?? "[]");
      const storedIds = Array.isArray(parsed)
        ? parsed.filter((item): item is string => typeof item === "string")
        : [];
      if (currentProjectId && !storedIds.includes(currentProjectId)) storedIds.push(currentProjectId);
      setExpandedProjectIds(storedIds);
    } catch {
      setExpandedProjectIds([]);
    } finally {
      loaded.current = true;
    }
  }, []);

  useEffect(() => {
    if (!loaded.current) return;
    try {
      localStorage.setItem(PROJECT_STORAGE_KEY, JSON.stringify(expandedProjectIds));
    } catch {
      // Browser storage can be disabled; Collapse remains usable in memory.
    }
  }, [expandedProjectIds]);

  const projectIdsKey = projectIds.join("\u0000");

  useEffect(() => {
    if (!projectsLoaded) return;
    const knownProjectIds = new Set(projectIds);
    setExpandedProjectIds((previous) => previous.filter((projectId) => knownProjectIds.has(projectId)));
  }, [projectIdsKey, projectsLoaded]);

  useEffect(() => {
    if (previousCurrentProjectId.current === currentProjectId) return;
    previousCurrentProjectId.current = currentProjectId;
    if (!currentProjectId) return;
    setExpandedProjectIds((previous) => previous.includes(currentProjectId) ? previous : [...previous, currentProjectId]);
  }, [currentProjectId]);

  return { expandedProjectIds, setExpandedProjectIds };
}
