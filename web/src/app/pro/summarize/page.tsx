"use client";
import { useState, useEffect } from "react";
import { fetcher, postApi } from "@/lib/api";

export default function ProSummarize() {
  const [proEnabled, setProEnabled] = useState(false);
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
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
      const res = await postApi("/api/pro/summarize", { text, title: title || undefined });
      setResult(res);
    } catch (err: any) {
      setResult({ error: err.message });
    }
    setLoading(false);
  };

  if (!proEnabled) {
    return (
      <div>
        <h1 className="text-2xl font-bold mb-4">AI Summarize</h1>
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
        AI Summarize{" "}
        <span className="text-sm bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full ml-2">
          PRO
        </span>
      </h1>

      <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow p-6 mb-6">
        <div className="mb-4">
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Title (optional)
          </label>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Document title"
            className="w-full px-4 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-purple-500"
          />
        </div>

        <div className="mb-4">
          <label className="block text-sm font-medium text-gray-700 mb-1">Text</label>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Paste the document text here..."
            required
            rows={10}
            className="w-full px-4 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-purple-500 resize-y"
          />
        </div>

        <button
          type="submit"
          disabled={loading || !text.trim()}
          className="bg-purple-600 text-white px-6 py-2 rounded-lg hover:bg-purple-700 disabled:opacity-50 text-sm"
        >
          {loading ? "Summarizing..." : "Summarize"}
        </button>
      </form>

      {result && !result.error && (
        <div className="bg-white rounded-lg shadow p-6">
          <h2 className="text-sm font-semibold text-gray-500 mb-2">Summary</h2>
          <p className="text-gray-800 leading-relaxed">{result.summary}</p>
          {result.tokens_used && (
            <p className="text-xs text-gray-400 mt-3">Tokens used: {result.tokens_used}</p>
          )}
        </div>
      )}

      {result?.error && (
        <div className="bg-red-50 rounded-lg p-4">
          <p className="text-red-700 text-sm">{result.error}</p>
        </div>
      )}
    </div>
  );
}
