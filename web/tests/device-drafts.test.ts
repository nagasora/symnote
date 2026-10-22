import { test } from "node:test";
import assert from "node:assert/strict";
import { indexedDB as fakeDatabase } from "fake-indexeddb";
import { readDeviceDraft, saveDeviceDraft } from "../src/nativeDrafts.ts";

test("device draft survives reopen; a storage failure cannot be reported as saved", async () => {
  globalThis.indexedDB = fakeDatabase;
  assert.equal(await readDeviceDraft(), null);
  const text = "ネットなしの日本語メモ\n改行も保持";
  const saved = await saveDeviceDraft(text);
  assert.equal(saved.text, text);
  assert.ok(Number.isFinite(Date.parse(saved.savedAt)));
  assert.deepEqual(await readDeviceDraft(), saved);
  globalThis.indexedDB = { open() { throw new Error("Storage unavailable"); } } as unknown as IDBFactory;
  await assert.rejects(() => saveDeviceDraft("保存に失敗する変更"), /Storage unavailable/);
  globalThis.indexedDB = fakeDatabase;
  assert.deepEqual(await readDeviceDraft(), saved);
});
