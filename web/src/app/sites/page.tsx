"use client";
import useSWR from "swr";
import { fetcher, postApi } from "@/lib/api";
import { useState } from "react";
import Link from "next/link";

interface Site {
  id: string;
  name: string;
  paper_count: number;
  last_crawled: string | null;
  country: string;
  crawl_status: string; // "success" | "failed" | "not_tested"
  crawl_status_reason: string | null;
}

type CountryCode = "KR" | "US" | "DE" | "GB" | "OTHER";
type StatusFilter = "all" | "success" | "failed" | "not_tested";

const COUNTRY_INFO: Record<CountryCode, { name: string; flag: string; nameKo: string; color: string }> = {
  KR: { name: "South Korea", flag: "🇰🇷", nameKo: "대한민국", color: "#0047A0" },
  US: { name: "United States", flag: "🇺🇸", nameKo: "미국", color: "#002868" },
  DE: { name: "Germany", flag: "🇩🇪", nameKo: "독일", color: "#000000" },
  GB: { name: "United Kingdom", flag: "🇬🇧", nameKo: "영국", color: "#012169" },
  OTHER: { name: "Other", flag: "🌐", nameKo: "기타", color: "#6B7280" },
};

const COUNTRY_ORDER: CountryCode[] = ["KR", "US", "DE", "GB", "OTHER"];

const STATUS_TABS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "전체" },
  { value: "success", label: "성공" },
  { value: "failed", label: "실패" },
  { value: "not_tested", label: "미테스트" },
];

function StatusBadge({ status }: { status: string }) {
  if (status === "success") {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-700">
        성공
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-700">
        실패
      </span>
    );
  }
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-500">
      미테스트
    </span>
  );
}

export default function SitesList() {
  const { data: sites, error } = useSWR<Site[]>("/api/sites", fetcher);
  const [selectedCountry, setSelectedCountry] = useState<CountryCode | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [crawlStates, setCrawlStates] = useState<Record<string, "idle" | "started">>({});

  const handleCrawl = async (siteId: string) => {
    try {
      await postApi(`/api/crawl/${siteId}`);
      setCrawlStates((prev) => ({ ...prev, [siteId]: "started" }));
      setTimeout(() => setCrawlStates((prev) => ({ ...prev, [siteId]: "idle" })), 2000);
    } catch {
      // ignore
    }
  };

  if (error) return <div className="text-red-500">Failed to load sites</div>;
  if (!sites) return <div className="text-gray-500">Loading...</div>;

  // Group sites by country
  const grouped = sites.reduce<Record<string, Site[]>>((acc, site) => {
    const key = site.country in COUNTRY_INFO ? site.country : "OTHER";
    if (!acc[key]) acc[key] = [];
    acc[key].push(site);
    return acc;
  }, {});

  // --- Country grid view ---
  if (!selectedCountry) {
    return (
      <div>
        <div className="mb-6">
          <h1 className="text-2xl font-bold">Sites</h1>
          <p className="text-sm text-gray-500">{sites.length} sites across {Object.keys(grouped).length} countries</p>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          {COUNTRY_ORDER.filter((code) => grouped[code]?.length > 0).map((code) => {
            const info = COUNTRY_INFO[code];
            const countrySites = grouped[code] ?? [];
            const totalPapers = countrySites.reduce((sum, s) => sum + s.paper_count, 0);
            const successCount = countrySites.filter((s) => s.crawl_status === "success").length;
            const failedCount = countrySites.filter((s) => s.crawl_status === "failed").length;
            const notTestedCount = countrySites.filter((s) => s.crawl_status === "not_tested").length;

            return (
              <button
                key={code}
                onClick={() => { setSelectedCountry(code); setSearch(""); setStatusFilter("all"); }}
                className="text-left bg-white rounded-lg shadow hover:shadow-md transition-shadow duration-200 border-l-4 p-5 focus:outline-none focus:ring-2 focus:ring-blue-300"
                style={{ borderLeftColor: info.color }}
              >
                <div className="text-5xl mb-3">{info.flag}</div>
                <div className="font-semibold text-gray-800 text-base leading-tight">{info.name}</div>
                <div className="text-sm text-gray-500 mb-3">({info.nameKo})</div>
                <div className="flex gap-4 text-sm mb-3">
                  <div>
                    <span className="font-bold text-gray-800">{countrySites.length}</span>
                    <span className="text-gray-500 ml-1">sites</span>
                  </div>
                  <div>
                    <span className="font-bold text-gray-800">{totalPapers.toLocaleString()}</span>
                    <span className="text-gray-500 ml-1">papers</span>
                  </div>
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
                  {successCount > 0 && (
                    <span className="text-green-600 font-medium">성공 {successCount}</span>
                  )}
                  {failedCount > 0 && (
                    <span className="text-red-500 font-medium">실패 {failedCount}</span>
                  )}
                  {notTestedCount > 0 && (
                    <span className="text-gray-400 font-medium">미테스트 {notTestedCount}</span>
                  )}
                </div>
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  // --- Site list view for selected country ---
  const info = COUNTRY_INFO[selectedCountry];
  const countrySites = grouped[selectedCountry] ?? [];

  const successCount = countrySites.filter((s) => s.crawl_status === "success").length;
  const failedCount = countrySites.filter((s) => s.crawl_status === "failed").length;
  const notTestedCount = countrySites.filter((s) => s.crawl_status === "not_tested").length;

  const tabCounts: Record<StatusFilter, number> = {
    all: countrySites.length,
    success: successCount,
    failed: failedCount,
    not_tested: notTestedCount,
  };

  const filtered = countrySites.filter((s) => {
    const matchesSearch =
      s.name.toLowerCase().includes(search.toLowerCase()) ||
      s.id.toLowerCase().includes(search.toLowerCase());
    const matchesStatus =
      statusFilter === "all" || s.crawl_status === statusFilter;
    return matchesSearch && matchesStatus;
  });

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <button
            onClick={() => { setSelectedCountry(null); setSearch(""); setStatusFilter("all"); }}
            className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            All Countries
          </button>
          <span className="text-gray-300">/</span>
          <div className="flex items-center gap-2">
            <span className="text-2xl">{info.flag}</span>
            <div>
              <h1 className="text-2xl font-bold leading-tight">{info.name}</h1>
              <p className="text-sm text-gray-500">{info.nameKo} · {countrySites.length} sites</p>
            </div>
          </div>
        </div>
        <input
          type="text"
          placeholder="Search by name or ID..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="border rounded-lg px-3 py-2 text-sm w-64 focus:outline-none focus:ring-2 focus:ring-blue-300"
        />
      </div>

      {/* Status filter tabs */}
      <div className="flex gap-2 mb-4">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab.value}
            onClick={() => setStatusFilter(tab.value)}
            className={`px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
              statusFilter === tab.value
                ? "bg-blue-600 text-white"
                : "bg-gray-100 text-gray-600 hover:bg-gray-200"
            }`}
          >
            {tab.label}
            <span
              className={`ml-1.5 text-xs ${
                statusFilter === tab.value ? "text-blue-200" : "text-gray-400"
              }`}
            >
              {tabCounts[tab.value]}
            </span>
          </button>
        ))}
      </div>

      <div className="bg-white rounded-lg shadow overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b">
            <tr>
              <th className="text-left px-4 py-3 font-medium text-gray-600">Site ID</th>
              <th className="text-left px-4 py-3 font-medium text-gray-600">Name</th>
              <th className="text-right px-4 py-3 font-medium text-gray-600">Papers</th>
              <th className="text-center px-4 py-3 font-medium text-gray-600">Status</th>
              <th className="text-right px-4 py-3 font-medium text-gray-600">Last Crawled</th>
              <th className="text-right px-4 py-3 font-medium text-gray-600">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-4 py-6 text-center text-gray-400">
                  No sites match your search.
                </td>
              </tr>
            ) : (
              filtered.map((site) => (
                <tr key={site.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-gray-500 font-mono text-xs">{site.id}</td>
                  <td className="px-4 py-3">
                    <Link href={`/sites/${site.id}`} className="text-blue-600 hover:underline font-medium">
                      {site.name}
                    </Link>
                    {site.crawl_status === "failed" && site.crawl_status_reason && (
                      <p className="text-xs text-red-400 mt-0.5">{site.crawl_status_reason}</p>
                    )}
                  </td>
                  <td className="text-right px-4 py-3">{site.paper_count.toLocaleString()}</td>
                  <td className="text-center px-4 py-3">
                    <StatusBadge status={site.crawl_status} />
                  </td>
                  <td className="text-right px-4 py-3 text-gray-500">
                    {site.last_crawled ? site.last_crawled.split("T")[0] : "-"}
                  </td>
                  <td className="text-right px-4 py-3">
                    <button
                      onClick={() => handleCrawl(site.id)}
                      className="text-xs px-2 py-1 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                      disabled={crawlStates[site.id] === "started"}
                    >
                      {crawlStates[site.id] === "started" ? "Started!" : "Crawl"}
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {(search || statusFilter !== "all") && (
        <p className="text-sm text-gray-400 mt-3">
          Showing {filtered.length} of {countrySites.length} sites
        </p>
      )}
    </div>
  );
}
