const store = new Map<string, { value: unknown; expiresAt: number }>();

export function cached<T>(key: string, ttlMs: number, compute: () => T): T {
  const now = Date.now();
  const entry = store.get(key);
  if (entry && entry.expiresAt > now) {
    return entry.value as T;
  }
  const value = compute();
  store.set(key, { value, expiresAt: now + ttlMs });
  return value;
}

export function invalidate(prefix?: string): void {
  if (prefix === undefined) {
    store.clear();
    return;
  }
  for (const key of store.keys()) {
    if (key.startsWith(prefix)) {
      store.delete(key);
    }
  }
}
