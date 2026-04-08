"use client";
import { useState } from "react";
import { postApi } from "@/lib/api";

export default function CrawlPreview() {
  const [url, setUrl] = useState("");
  const [fetchMethod, setFetchMethod] = useState("");
  const [selectors, setSelectors] = useState({
    item_container: "",
    item_link: "a",
    item_link_attr: "href",
    title: "",
    date: "",
  });
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState("");
  const [recommending, setRecommending] = useState(false);
  const [recommendations, setRecommendations] = useState<any[]>([]);

  const handleAutoRecommend = async () => {
    if (!url) return;
    setRecommending(true);
    setRecommendations([]);
    try {
      const res = await postApi("/api/auto-selectors", { url, fetch_method: fetchMethod || null });
      if (res.recommendations) {
        setRecommendations(res.recommendations);
      }
    } catch (err: any) {
      // ignore
    } finally {
      setRecommending(false);
    }
  };

  const applyRecommendation = (rec: any) => {
    setSelectors({
      item_container: rec.selectors.item_container || "",
      item_link: rec.selectors.item_link || "a",
      item_link_attr: rec.selectors.item_link_attr || "href",
      title: rec.selectors.title || "",
      date: rec.selectors.date || "",
    });
    setRecommendations([]);
  };

  const handleTest = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const res = await postApi("/api/crawl-preview", {
        url,
        selectors,
        fetch_method: fetchMethod || null,
      });
      setResult(res);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">크롤링 미리보기</h1>
      <p className="text-sm text-gray-500 mb-6">URL과 CSS 셀렉터를 입력하면 실제로 어떤 데이터가 추출되는지 미리 확인할 수 있습니다.</p>

      <form onSubmit={handleTest} className="bg-white rounded-lg shadow p-6 mb-6 space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">URL</label>
          <input type="url" required value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/board/list.do"
            className="w-full px-4 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
        </div>

        <div className="flex gap-3">
          <div className="flex-1">
            <label className="block text-sm font-medium text-gray-700 mb-1">가져오기 방법</label>
            <select value={fetchMethod} onChange={(e) => setFetchMethod(e.target.value)}
              className="w-full px-3 py-2 border rounded-lg text-sm">
              <option value="">기본 (requests)</option>
              <option value="browser">브라우저 (JS 렌더링)</option>
              <option value="cloudscraper">Cloudscraper (WAF 우회)</option>
            </select>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">항목 컨테이너 (item_container)</label>
            <input type="text" value={selectors.item_container}
              onChange={(e) => setSelectors({...selectors, item_container: e.target.value})}
              placeholder="예: table tbody tr, li.item, div.board-list > div"
              className="w-full px-3 py-2 border rounded-lg text-sm font-mono" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">링크 셀렉터 (item_link)</label>
            <input type="text" value={selectors.item_link}
              onChange={(e) => setSelectors({...selectors, item_link: e.target.value})}
              placeholder="예: a, a.title"
              className="w-full px-3 py-2 border rounded-lg text-sm font-mono" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">제목 셀렉터 (title)</label>
            <input type="text" value={selectors.title}
              onChange={(e) => setSelectors({...selectors, title: e.target.value})}
              placeholder="예: a.title, h3, td.subject a"
              className="w-full px-3 py-2 border rounded-lg text-sm font-mono" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">날짜 셀렉터 (date)</label>
            <input type="text" value={selectors.date}
              onChange={(e) => setSelectors({...selectors, date: e.target.value})}
              placeholder="예: span.date, td.date"
              className="w-full px-3 py-2 border rounded-lg text-sm font-mono" />
          </div>
        </div>

        <div className="flex gap-2">
          <button type="button" onClick={handleAutoRecommend} disabled={recommending || !url}
            className="bg-green-600 text-white px-6 py-2 rounded-lg hover:bg-green-700 text-sm disabled:opacity-50">
            {recommending ? "분석 중..." : "🎯 셀렉터 자동 추천"}
          </button>
          <button type="submit" disabled={loading}
            className="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700 text-sm disabled:opacity-50">
            {loading ? "테스트 중..." : "미리보기"}
          </button>
        </div>
      </form>

      {recommendations.length > 0 && (
        <div className="bg-white rounded-lg shadow p-4 mb-4">
          <h3 className="font-medium mb-3">🎯 추천 셀렉터 ({recommendations.length}개)</h3>
          <div className="space-y-2">
            {recommendations.map((rec, i) => (
              <div key={i} onClick={() => applyRecommendation(rec)}
                className="border rounded-lg p-3 cursor-pointer hover:bg-blue-50 hover:border-blue-300 transition">
                <div className="flex justify-between items-center">
                  <code className="text-sm font-mono text-blue-700">{rec.selectors.item_container}</code>
                  <span className="text-xs text-gray-500">{rec.items_found}개 항목</span>
                </div>
                {rec.sample_title && (
                  <p className="text-xs text-gray-500 mt-1">예시: {rec.sample_title}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {error && <div className="text-red-500 mb-4">{error}</div>}

      {loading && (
        <div className="bg-white rounded-lg shadow p-8 text-center">
          <div className="animate-spin h-8 w-8 border-4 border-blue-500 border-t-transparent rounded-full mx-auto mb-4"></div>
          <p className="text-gray-500">페이지를 가져와서 셀렉터를 테스트하고 있습니다...</p>
        </div>
      )}

      {result && (
        <div>
          {/* Summary */}
          <div className={`rounded-lg p-4 mb-4 ${result.total_items > 0 ? "bg-green-50 border border-green-200" : "bg-yellow-50 border border-yellow-200"}`}>
            <p className="font-medium">
              {result.total_items > 0
                ? `${result.total_items}개 항목 발견 (방법: ${result.method_used}, HTML: ${(result.html_size/1024).toFixed(1)}KB)`
                : `항목을 찾지 못했습니다 (HTML: ${(result.html_size/1024).toFixed(1)}KB)`
              }
            </p>
          </div>

          {/* Diagnosis hints */}
          {result.diagnosis && result.diagnosis.length > 0 && (
            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4">
              <h3 className="font-medium text-blue-800 mb-2">진단 힌트</h3>
              <ul className="list-disc list-inside text-sm text-blue-700 space-y-1">
                {result.diagnosis.map((hint: string, i: number) => (
                  <li key={i}>{hint}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Selector results */}
          {result.selector_results && (
            <div className="bg-white rounded-lg shadow p-4 mb-4">
              <h3 className="font-medium mb-2">셀렉터별 매칭 결과</h3>
              <table className="w-full text-sm">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="text-left px-3 py-2">셀렉터</th>
                    <th className="text-right px-3 py-2">매칭 수</th>
                    <th className="text-left px-3 py-2">샘플</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {Object.entries(result.selector_results).map(([key, val]: [string, any]) => (
                    <tr key={key}>
                      <td className="px-3 py-2 font-mono text-xs">{key}</td>
                      <td className={`text-right px-3 py-2 ${val.count > 0 ? "text-green-600" : "text-red-500"}`}>{val.count}</td>
                      <td className="px-3 py-2 text-gray-500 text-xs truncate max-w-xs">{val.sample || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Preview items */}
          {result.items && result.items.length > 0 && (
            <div className="bg-white rounded-lg shadow overflow-hidden">
              <h3 className="font-medium p-4 border-b">추출된 항목 ({result.items.length}개)</h3>
              <table className="w-full text-sm">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="text-left px-4 py-2">#</th>
                    <th className="text-left px-4 py-2">제목</th>
                    <th className="text-left px-4 py-2">링크</th>
                    <th className="text-left px-4 py-2">날짜</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {result.items.map((item: any, i: number) => (
                    <tr key={i} className="hover:bg-gray-50">
                      <td className="px-4 py-2 text-gray-400">{i + 1}</td>
                      <td className="px-4 py-2">{item.title || <span className="text-red-400">(빈 제목)</span>}</td>
                      <td className="px-4 py-2 text-xs text-gray-500 truncate max-w-xs font-mono">{item.link || "-"}</td>
                      <td className="px-4 py-2 text-gray-500">{item.date || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
