"use client";
import { useState, useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import { fetcher } from "@/lib/api";
import useSWR from "swr";

export default function ConfigEditor() {
  const params = useParams();
  const router = useRouter();
  const siteId = params.siteId as string;
  const { data: config, error } = useSWR(`/api/config/${siteId}`, fetcher);
  const [jsonText, setJsonText] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [parseError, setParseError] = useState("");

  useEffect(() => {
    if (config) {
      setJsonText(JSON.stringify(config, null, 2));
    }
  }, [config]);

  const handleSave = async () => {
    try {
      JSON.parse(jsonText); // validate
      setParseError("");
    } catch (e: any) {
      setParseError(`JSON 형식 오류: ${e.message}`);
      return;
    }

    setSaving(true);
    setMessage("");
    try {
      const res = await fetch(`/api/config/${siteId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: jsonText,
      });
      if (!res.ok) throw new Error(`${res.status}`);
      setMessage("저장 완료!");
      setTimeout(() => setMessage(""), 3000);
    } catch (e: any) {
      setMessage(`저장 실패: ${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  if (error) return <div className="text-red-500">설정을 불러올 수 없습니다</div>;
  if (!config) return <div className="text-gray-500">로딩 중...</div>;

  // Extract key options for quick toggles
  let parsed: any = {};
  try { parsed = JSON.parse(jsonText); } catch {}
  const options = parsed?.options || {};

  const toggleOption = (key: string, value: any) => {
    try {
      const obj = JSON.parse(jsonText);
      if (!obj.options) obj.options = {};
      obj.options[key] = value;
      setJsonText(JSON.stringify(obj, null, 2));
    } catch {}
  };

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <button onClick={() => router.back()} className="text-blue-600 hover:underline text-sm">&larr; 뒤로</button>
        <h1 className="text-2xl font-bold">크롤러 설정 편집</h1>
        <span className="text-sm text-gray-400 font-mono">{siteId}</span>
      </div>

      {/* Quick toggles */}
      <div className="bg-white rounded-lg shadow p-4 mb-4 flex flex-wrap gap-4 text-sm">
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox"
            checked={options.fetch_method === "browser"}
            onChange={(e) => toggleOption("fetch_method", e.target.checked ? "browser" : null)}
            className="w-4 h-4" />
          브라우저 모드 (JS 렌더링)
        </label>
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox"
            checked={options.respect_robots === false}
            onChange={(e) => toggleOption("respect_robots", !e.target.checked)}
            className="w-4 h-4" />
          robots.txt 무시
        </label>
        <label className="flex items-center gap-2">
          딜레이:
          <input type="number" step="0.5" min="0.5" max="10"
            value={options.delay || 1.5}
            onChange={(e) => toggleOption("delay", parseFloat(e.target.value))}
            className="w-20 border rounded px-2 py-1" />
          초
        </label>
      </div>

      {/* JSON editor */}
      <div className="bg-white rounded-lg shadow p-4 mb-4">
        <textarea
          value={jsonText}
          onChange={(e) => { setJsonText(e.target.value); setParseError(""); }}
          className="w-full h-96 font-mono text-sm border rounded-lg p-3 focus:outline-none focus:ring-2 focus:ring-blue-500"
          spellCheck={false}
        />
        {parseError && <p className="text-red-500 text-sm mt-2">{parseError}</p>}
      </div>

      <div className="flex items-center gap-3">
        <button onClick={handleSave} disabled={saving}
          className="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700 text-sm disabled:opacity-50">
          {saving ? "저장 중..." : "설정 저장"}
        </button>
        {message && <span className={`text-sm ${message.includes("실패") ? "text-red-500" : "text-green-600"}`}>{message}</span>}
      </div>
    </div>
  );
}
