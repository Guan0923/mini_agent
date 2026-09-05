import { afterEach, describe, expect, it, vi } from "vitest";

import { fileReferenceAvailable, sessionFileContentUrl } from "./files";

afterEach(() => vi.unstubAllGlobals());

describe("sessionFileContentUrl", () => {
  it("omits the optional download query for normal access", () => {
    expect(sessionFileContentUrl("session 1", "workspace", "workspace:notes/read me.txt"))
      .toBe("/api/sessions/session%201/files/content?source=workspace&path=workspace%3Anotes%2Fread+me.txt");
  });

  it("adds download=true only for an explicit download", () => {
    expect(sessionFileContentUrl("session_1", "project", "project:\u4e2d\u6587 \u6587\u4ef6.txt", true))
      .toBe("/api/sessions/session_1/files/content?source=project&path=project%3A%E4%B8%AD%E6%96%87+%E6%96%87%E4%BB%B6.txt&download=true");
  });
});

describe("fileReferenceAvailable", () => {
  it("probes the normal content URL without an empty download query", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fileReferenceAvailable(
      { source: "workspace", path: "workspace:plants-vs-zombies.html", display_path: "workspace:plants-vs-zombies.html" },
      "session_1",
    )).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions/session_1/files/content?source=workspace&path=workspace%3Aplants-vs-zombies.html",
      { method: "HEAD", cache: "no-store" },
    );
  });

  it("reports a missing file as unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));

    await expect(fileReferenceAvailable(
      { source: "workspace", path: "workspace:missing.txt", display_path: "workspace:missing.txt" },
      "session_1",
    )).resolves.toBe(false);
  });
});
