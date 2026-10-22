import { test } from "node:test";
import assert from "node:assert/strict";
import { makeNativeRequester } from "../src/nativeTransport.ts";

test("native transport permits API operations but never credential exchange or off-origin paths", async () => {
  const calls: unknown[] = [];
  const request = makeNativeRequester({ async request(options) {
    calls.push(options);
    return { status: 200, data: { ok: true } };
  } });
  await request("/api/tasks?limit=10", "get");
  await request("/api/memos", "post", { text: "端末からのメモ" });
  assert.deepEqual(calls, [
    { path: "/api/tasks?limit=10", method: "GET", body: undefined },
    { path: "/api/memos", method: "POST", body: { text: "端末からのメモ" } },
  ]);
  for (const path of ["https://evil.test/api/tasks", "//evil.test/api/tasks", "/api/../../private", "/api/auth/logout", "/api/auth/mobile/exchange", "/api/auth%2fmobile/exchange", "/api/x/../auth/google/start", "/api/%252e%252e/private", "/api/tasks#fragment"]) {
    await assert.rejects(() => request(path, "GET"));
  }
  await assert.rejects(() => request("/api/tasks", "TRACE"));
  assert.equal(calls.length, 2);
});
