export interface DeviceDraft {
  text: string;
  savedAt: string;
}

const DATABASE = "symnote-device-drafts";
const STORE = "drafts";
const KEY = "quick-capture";

async function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    let blocked = false;
    const request = indexedDB.open(DATABASE, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => {
      if (blocked) request.result.close();
      else resolve(request.result);
    };
    request.onerror = () => reject(request.error ?? new Error("端末内の保存領域を開けませんでした。"));
    request.onblocked = () => {
      blocked = true;
      reject(new Error("他の画面を閉じて、もう一度お試しください。"));
    };
  });
}

export async function readDeviceDraft(): Promise<DeviceDraft | null> {
  const db = await openDatabase();
  try {
    return await new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE, "readonly");
      const request = transaction.objectStore(STORE).get(KEY);
      transaction.oncomplete = () => resolve(request.result ?? null);
      transaction.onabort = () => reject(transaction.error ?? new Error("メモを読み込めませんでした。"));
      transaction.onerror = () => reject(transaction.error ?? new Error("メモを読み込めませんでした。"));
    });
  } finally {
    db.close();
  }
}

export async function saveDeviceDraft(text: string): Promise<DeviceDraft> {
  const draft = { text, savedAt: new Date().toISOString() };
  const db = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = db.transaction(STORE, "readwrite");
      transaction.objectStore(STORE).put(draft, KEY);
      transaction.oncomplete = () => resolve();
      transaction.onabort = () => reject(transaction.error ?? new Error("メモを保存できませんでした。"));
      transaction.onerror = () => reject(transaction.error ?? new Error("メモを保存できませんでした。"));
    });
    return draft;
  } finally {
    db.close();
  }
}
