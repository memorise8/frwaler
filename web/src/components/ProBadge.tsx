"use client";
import { useEffect, useState } from "react";
import { getProStatus } from "@/lib/api";

export default function ProBadge() {
  const [proEnabled, setProEnabled] = useState<boolean | null>(null);

  useEffect(() => {
    getProStatus()
      .then((s) => setProEnabled(s.pro_enabled))
      .catch(() => setProEnabled(false));
  }, []);

  if (!proEnabled) return null;

  return (
    <span className="bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full text-xs font-semibold tracking-wide">
      PRO
    </span>
  );
}
