"use client";
import { useState, useEffect } from "react";
import { fetcher, postApi } from "@/lib/api";

export default function ProAutoAdd() {
  const [proEnabled, setProEnabled] = useState(false);
  const [url, setUrl] = useState("");
  const [browser, setBrowser] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);

  useEffect(() => {
    fetcher("/api/pro/status")
      .then((s) => setProEnabled(s.pro_enabled))
      .catch(() => {});
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setResult(null);
    try {
      const res = await postApi("/api/pro/auto-add", { url, browser });
      setResult(res);
    } catch (err: any) {
      setResult({ success: false, reason: err.message });
    }
    setLoading(false);
  };

  if (!proEnabled) {
    return (
      <div>
        <h1 className="text-2xl font-bold mb-4">Auto-Add Site</h1>
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-6">
          <p className="text-yellow-800 font-medium">PRO mode required</p>
          <p className="text-yellow-600 text-sm mt-1">
            Configure your license key in{" "}
            <a href="/settings" className="underline">
              Settings
            </a>
            .
          </p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">
        Auto-Add Site{" "}
        <span className="text-sm bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full ml-2">
          PRO
        </span>
      </h1>

      <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow p-6 mb-6">
        <div className="mb-4">
          <label className="block text-sm font-medium text-gray-700 mb-1">URL</label>
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/publications"
            required
            className="w-full px-4 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-purple-500"
          />
        </div>

        <div className="mb-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={browser}
              onChange={(e) => setBrowser(e.target.checked)}
              className="rounded"
            />
            <span>Use browser rendering (for SPA/React sites)</span>
          </label>
        </div>

        <button
          type="submit"
          disabled={loading}
          className="bg-purple-600 text-white px-6 py-2 rounded-lg hover:bg-purple-700 disabled:opacity-50 text-sm"
        >
          {loading ? "Analyzing..." : "Auto-Add"}
        </button>
      </form>

      {result && (
        <div
          className={`rounded-lg shadow p-6 ${result.success ? "bg-green-50" : "bg-red-50"}`}
        >
          <p
            className={`font-medium ${result.success ? "text-green-700" : "text-red-700"}`}
          >
            {result.success ? "Success!" : "Failed"}
          </p>
          {result.site_id && (
            <p className="text-sm text-gray-600 mt-1">Site ID: {result.site_id}</p>
          )}
          {result.reason && (
            <p className="text-sm text-gray-500 mt-1">{result.reason}</p>
          )}
        </div>
      )}
    </div>
  );
}
