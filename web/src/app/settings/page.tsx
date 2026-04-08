"use client";
import { useState, useEffect } from "react";
import { fetcher } from "@/lib/api";

export default function Settings() {
  const [proStatus, setProStatus] = useState<any>(null);
  const [usage, setUsage] = useState<any>(null);

  useEffect(() => {
    fetcher("/api/pro/status").then(setProStatus).catch(() => {});
    fetcher("/api/pro/usage").then(setUsage).catch(() => {});
  }, []);

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Settings</h1>

      <div className="bg-white rounded-lg shadow p-6 mb-6">
        <h2 className="text-lg font-semibold mb-4">PRO Mode</h2>

        {proStatus?.pro_enabled ? (
          <div>
            <div className="flex items-center gap-2 mb-4">
              <span className="bg-green-100 text-green-700 px-3 py-1 rounded-full text-sm font-medium">
                PRO Active
              </span>
              <span className="text-sm text-gray-500">Plan: {proStatus.plan}</span>
            </div>

            {usage && (
              <div className="grid grid-cols-2 gap-4">
                <div className="bg-gray-50 rounded p-4">
                  <p className="text-sm text-gray-500">Today</p>
                  <p className="text-xl font-bold">
                    {usage.daily?.requests} / {usage.daily?.limit}
                  </p>
                  <p className="text-xs text-gray-400">requests</p>
                </div>
                <div className="bg-gray-50 rounded p-4">
                  <p className="text-sm text-gray-500">This Month</p>
                  <p className="text-xl font-bold">
                    {usage.monthly?.requests} / {usage.monthly?.limit}
                  </p>
                  <p className="text-xs text-gray-400">requests</p>
                </div>
              </div>
            )}
          </div>
        ) : (
          <div>
            <p className="text-gray-500 mb-4">PRO mode is not active.</p>
            <p className="text-sm text-gray-400">Set environment variables to enable:</p>
            <pre className="bg-gray-50 rounded p-3 text-xs mt-2">
              CRAWLER_PRO_API_URL=https://pro.your-domain.com{"\n"}
              CRAWLER_PRO_LICENSE_KEY=your-key-here
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
