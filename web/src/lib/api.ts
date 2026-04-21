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

// ─── Pro Products API ────────────────────────────────────────────────────────

export async function listProducts(
  params: {
    device_type?: string;
    site_id?: string;
    brand?: string;
    q?: string;
    page?: number;
    per_page?: number;
  },
  licenseKey: string
) {
  const searchParams = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined) searchParams.set(k, String(v));
  });
  const res = await fetch(
    `${PRO_API_BASE}/pro/api/products?${searchParams}`,
    { headers: { "X-License-Key": licenseKey } }
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getProductStats(licenseKey: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/products/stats`, {
    headers: { "X-License-Key": licenseKey },
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// ─── Transistor Up-Screening API ─────────────────────────────────────────────

// Use relative paths — Next.js rewrites proxy /pro/api/* to the backend
const PRO_API_BASE = "";

export async function screenMosfetByFile(
  file: File,
  opts: { mpn?: string; manufacturer?: string; licenseKey: string }
) {
  const fd = new FormData();
  fd.append("file", file);
  if (opts.mpn) fd.append("mpn", opts.mpn);
  if (opts.manufacturer) fd.append("manufacturer", opts.manufacturer);
  const res = await fetch(`${PRO_API_BASE}/pro/api/screen-mosfet`, {
    method: "POST",
    headers: { "X-License-Key": opts.licenseKey },
    body: fd,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function screenMosfetByMpn(
  mpn: string,
  licenseKey: string,
  manufacturer?: string
) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/screen-mosfet`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-License-Key": licenseKey,
    },
    body: JSON.stringify({ mpn, manufacturer }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function screenBjtByFile(
  file: File,
  opts: { mpn?: string; manufacturer?: string; licenseKey: string }
) {
  const fd = new FormData();
  fd.append("file", file);
  if (opts.mpn) fd.append("mpn", opts.mpn);
  if (opts.manufacturer) fd.append("manufacturer", opts.manufacturer);
  const res = await fetch(`${PRO_API_BASE}/pro/api/screen-bjt`, {
    method: "POST",
    headers: { "X-License-Key": opts.licenseKey },
    body: fd,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function screenBjtByMpn(
  mpn: string,
  licenseKey: string,
  manufacturer?: string
) {
  const fd = new FormData();
  fd.append("mpn", mpn);
  if (manufacturer) fd.append("manufacturer", manufacturer);
  const res = await fetch(`${PRO_API_BASE}/pro/api/screen-bjt`, {
    method: "POST",
    headers: { "X-License-Key": licenseKey },
    body: fd,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function screenMosfetByFile(
  file: File,
  opts: { mpn?: string; manufacturer?: string; licenseKey: string }
) {
  const fd = new FormData();
  fd.append("file", file);
  if (opts.mpn) fd.append("mpn", opts.mpn);
  if (opts.manufacturer) fd.append("manufacturer", opts.manufacturer);
  const res = await fetch(`${PRO_API_BASE}/pro/api/screen-mosfet`, {
    method: "POST",
    headers: { "X-License-Key": opts.licenseKey },
    body: fd,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function screenMosfetByMpn(
  mpn: string,
  licenseKey: string,
  manufacturer?: string
) {
  const fd = new FormData();
  fd.append("mpn", mpn);
  if (manufacturer) fd.append("manufacturer", manufacturer);
  const res = await fetch(`${PRO_API_BASE}/pro/api/screen-mosfet`, {
    method: "POST",
    headers: { "X-License-Key": licenseKey },
    body: fd,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getScreening(id: string, licenseKey: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/screen-bjt/${id}`, {
    headers: { "X-License-Key": licenseKey },
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function listFactors(licenseKey: string, partType = "bjt") {
  const res = await fetch(
    `${PRO_API_BASE}/pro/api/factors?part_type=${partType}`,
    { headers: { "X-License-Key": licenseKey } }
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function submitFeedback(
  reportId: string,
  rating: "up" | "down",
  licenseKey: string,
  factorName?: string,
  comment?: string
) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/feedback`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-License-Key": licenseKey,
    },
    body: JSON.stringify({
      report_id: reportId,
      rating,
      factor_name: factorName ?? null,
      comment: comment ?? null,
    }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getFeedback(reportId: string, licenseKey: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/feedback/${reportId}`, {
    headers: { "X-License-Key": licenseKey },
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// ─── User Account API ───────────────────────────────────────────────────────

export async function signup(email: string, name: string, password: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, name, password }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function login(email: string, password: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getMe(token: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getMyReports(token: string, page = 1, perPage = 20) {
  const res = await fetch(
    `${PRO_API_BASE}/pro/api/me/reports?page=${page}&per_page=${perPage}`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// ─── Admin API ───────────────────────────────────────────────────────────────

function adminHeaders(adminPassword: string) {
  return { "X-Admin-Password": adminPassword };
}

export async function adminListPending(adminPassword: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/admin/pending`, {
    headers: adminHeaders(adminPassword),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function adminListAll(adminPassword: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/admin/all`, {
    headers: adminHeaders(adminPassword),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function adminApprove(key: string, adminPassword: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/admin/approve/${key}`, {
    method: "POST",
    headers: adminHeaders(adminPassword),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function adminReject(key: string, adminPassword: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/admin/reject/${key}`, {
    method: "POST",
    headers: adminHeaders(adminPassword),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function adminDeactivate(key: string, adminPassword: string) {
  const res = await fetch(`${PRO_API_BASE}/pro/api/admin/deactivate/${key}`, {
    method: "POST",
    headers: adminHeaders(adminPassword),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
