"use client";
import { useState } from "react";
import { postApi } from "@/lib/api";
import Link from "next/link";

interface TestResult {
  method: string;
  status: number | null;
  size: number;
  time: number;
  captcha: boolean;
  error: string | null;
}

interface UrlTestResponse {
  url: string;
  results: TestResult[];
  best_method: string | null;
}

export default function UrlTest() {
  const [url, setUrl] = useState("");
  const [timeout, setTimeout_] = useState(15);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<UrlTestResponse | null>(null);
  const [error, setError] = useState("");

  const handleTest = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    setData(null);
    try {
      const res = await postApi("/api/url-test", { url, timeout });
      setData(res);
    } catch (err: any) {
      setError(err.message || "Test failed");
    } finally {
      setLoading(false);
    }
  };

  const isSuccess = (r: TestResult) => r.status && r.status < 400 && r.size >= 500 && !r.captcha;

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">URL Tester</h1>
      <p className="text-sm text-gray-500 mb-4">Test URL accessibility with 3 different methods: requests, cloudscraper, and headless browser</p>

      <form onSubmit={handleTest} className="bg-white rounded-lg shadow p-6 mb-6">
        <div className="flex gap-3">
          <input type="url" required value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/publications"
            className="flex-1 px-4 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
          <select value={timeout} onChange={(e) => setTimeout_(Number(e.target.value))}
            className="px-3 py-2 border rounded-lg text-sm">
            <option value={10}>10s</option>
            <option value={15}>15s</option>
            <option value={30}>30s</option>
            <option value={60}>60s</option>
          </select>
          <button type="submit" disabled={loading}
            className="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700 text-sm disabled:opacity-50 disabled:cursor-not-allowed">
            {loading ? "Testing..." : "Test URL"}
          </button>
        </div>
      </form>

      {error && <div className="text-red-500 mb-4">{error}</div>}

      {loading && (
        <div className="bg-white rounded-lg shadow p-8 text-center">
          <div className="animate-spin h-8 w-8 border-4 border-blue-500 border-t-transparent rounded-full mx-auto mb-4"></div>
          <p className="text-gray-500">Testing 3 methods... This may take up to {timeout * 3}s</p>
        </div>
      )}

      {data && (
        <div>
          {data.best_method && (
            <div className="bg-green-50 border border-green-200 rounded-lg p-4 mb-4">
              <p className="text-green-800 font-medium">Best method: <span className="font-bold">{data.best_method}</span></p>
              <Link href={`/auto-add?url=${encodeURIComponent(data.url)}`}
                className="inline-block mt-2 bg-green-600 text-white px-4 py-1.5 rounded text-sm hover:bg-green-700">
                Add This Site
              </Link>
            </div>
          )}
          {!data.best_method && (
            <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-4">
              <p className="text-red-800 font-medium">All methods failed. This URL may be geo-blocked or require special access.</p>
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {data.results.map((r) => (
              <div key={r.method} className={`bg-white rounded-lg shadow p-4 border-t-4 ${
                isSuccess(r) ? (r.method === data.best_method ? "border-green-500" : "border-green-300") : "border-red-400"
              }`}>
                <h3 className="font-semibold text-lg mb-2 capitalize">{r.method}</h3>
                <div className="space-y-1 text-sm">
                  <div className="flex justify-between">
                    <span className="text-gray-500">Status</span>
                    <span className={r.status && r.status < 400 ? "text-green-600 font-medium" : "text-red-600 font-medium"}>
                      {r.status ?? "Failed"}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">Size</span>
                    <span>{r.size > 0 ? `${(r.size / 1024).toFixed(1)} KB` : "-"}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">Time</span>
                    <span>{r.time}s</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">CAPTCHA</span>
                    <span className={r.captcha ? "text-yellow-600" : "text-gray-400"}>{r.captcha ? "Detected" : "No"}</span>
                  </div>
                  {r.error && <p className="text-red-500 text-xs mt-2 break-all">{r.error}</p>}
                </div>
                {r.method === data.best_method && (
                  <div className="mt-3 bg-green-100 text-green-800 text-xs font-medium px-2 py-1 rounded text-center">Recommended</div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
