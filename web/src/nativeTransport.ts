export interface NativeReply {
  status: number;
  data: unknown;
}

export interface NativeAPIPlugin {
  request(options: { path: string; method: string; body?: unknown }): Promise<NativeReply>;
}

export function makeNativeRequester(plugin: NativeAPIPlugin) {
  return async (path: string, method: string, body?: unknown): Promise<NativeReply> => {
    const url = new URL(path, "https://app.symnote.app");
    let decodedPath = url.pathname;
    for (let i = 0; i < 3; i++) decodedPath = decodeURIComponent(decodedPath);
    if (!path.startsWith("/api/") || path.includes("\\") || url.hash ||
        url.origin !== "https://app.symnote.app" || !decodedPath.startsWith("/api/") ||
        decodedPath.split("/").some((part) => part === "." || part === "..") ||
        decodedPath.startsWith("/api/auth/mobile") || decodedPath.startsWith("/api/auth/google") ||
        decodedPath === "/api/auth/logout") {
      throw new Error("この通信先は利用できません。");
    }
    const normalizedMethod = method.toUpperCase();
    if (!["GET", "POST", "PUT", "PATCH", "DELETE"].includes(normalizedMethod)) {
      throw new Error("この通信方法は利用できません。");
    }
    return plugin.request({ path, method: normalizedMethod, body });
  };
}
