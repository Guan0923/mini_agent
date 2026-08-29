import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { getCurrentUser, getGuestImportStatus, guestLogin as guestLoginRequest, login as loginRequest, logout as logoutRequest, resolveGuestImport, setUnauthorizedHandler } from "../api";
import { resetLegacyBrowserState } from "../app/storage";
import type { AuthUser } from "../types";

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  setUser: (user: AuthUser | null) => void;
  signIn: (email: string, password: string) => Promise<AuthUser>;
  signInGuest: () => Promise<AuthUser>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);
export function AuthProvider({ children }: { children: React.ReactNode }) {
  resetLegacyBrowserState();
  const [user, setUserState] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  const setUser = useCallback((next: AuthUser | null) => {
    setUserState(next);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    return () => setUnauthorizedHandler(null);
  }, [setUser]);

  useEffect(() => {
    let active = true;
    getCurrentUser()
      .then((next) => {
        if (active) setUser(next);
      })
      .catch(() => {
        if (active) setUser(null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [setUser]);

  const signIn = useCallback(
    async (email: string, password: string) => {
      const next = await loginRequest(email, password);
      setUser(next);
      return next;
    },
    [setUser],
  );

  const signInGuest = useCallback(async () => {
    const next = await guestLoginRequest();
    setUser(next);
    return next;
  }, [setUser]);

  useEffect(() => {
    if (!user || user.kind === "guest") return;
    let active = true;
    async function resolveWhenSyncReady(): Promise<void> {
      const latest = await getCurrentUser();
      if (latest && active) setUserState(latest);
      if (!active || !latest?.guest_import) return;
      const status = await getGuestImportStatus();
      if (!active || !status.available) return;
      const accepted = typeof window !== "undefined" && window.confirm(
        "检测到游客会话。是否只导入游客的会话、workspace 和上传文件？",
      );
      await resolveGuestImport(accepted ? "import" : "dismiss");
      if (active) setUserState((current) => current ? { ...current, guest_import: null } : current);
    }
    void resolveWhenSyncReady().catch(() => undefined);
    return () => { active = false; };
  }, [user?.id, user?.kind]);

  const signOut = useCallback(async () => {
    try {
      await logoutRequest();
    } finally {
      setUser(null);
    }
  }, [setUser]);

  const value = useMemo(() => ({ user, loading, setUser, signIn, signInGuest, signOut }), [user, loading, setUser, signIn, signInGuest, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
