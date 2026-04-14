const API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

export async function fetcher(url: string) {
  const res = await fetch(`${API_BASE}${url}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function postApi(url: string, body?: any) {
  const res = await fetch(`${API_BASE}${url}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function putApi(url: string, body?: any) {
  const res = await fetch(`${API_BASE}${url}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function getConfig(siteId: string) {
  return fetcher(`/api/config/${siteId}`);
}

export async function updateConfig(siteId: string, config: any) {
  return putApi(`/api/config/${siteId}`, config);
}

export async function getProStatus() {
  return fetcher("/api/pro/status");
}

export async function getProUsage() {
  return fetcher("/api/pro/usage");
}

export async function proAutoAdd(url: string, options?: { site_id?: string; browser?: boolean }) {
  return postApi("/api/pro/auto-add", { url, ...options });
}

export async function proSummarize(text: string, title?: string) {
  return postApi("/api/pro/summarize", { text, title });
}

export async function testUrl(url: string, timeout?: number) {
  return postApi("/api/url-test", { url, timeout });
}

export async function autoAdd(url: string, options?: { site_id?: string; browser?: boolean }) {
  return postApi("/api/auto-add", { url, ...options });
}

export async function startCrawl(siteId: string, limit?: number) {
  return postApi(`/api/crawl/${siteId}`, limit ? { limit } : undefined);
}

export async function smartFind(url: string, options?: { max_pages?: number; max_depth?: number; use_ai?: boolean }) {
  return postApi("/api/smart-find", { url, ...options });
}

export async function getFoundFiles(jobId: string) {
  return fetcher(`/api/smart-find/${jobId}/files`);
}

export async function downloadFoundFiles(jobId: string) {
  return postApi(`/api/smart-find/${jobId}/download`, {});
}

// Products
export async function fetchProducts(params?: Record<string, string>) {
  const query = params ? '?' + new URLSearchParams(params).toString() : '';
  const res = await fetch(`${API_BASE}/api/products${query}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchProduct(id: string) {
  const res = await fetch(`${API_BASE}/api/products/${id}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchProductHtml(id: string) {
  const res = await fetch(`${API_BASE}/api/products/${id}/html`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.text();
}

export async function fetchProductStats() {
  const res = await fetch(`${API_BASE}/api/products/stats`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}
