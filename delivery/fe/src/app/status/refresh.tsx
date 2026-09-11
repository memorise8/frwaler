"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function Refresh() {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => { if (!document.hidden) router.refresh(); }, 30000);
    return () => clearInterval(timer);
  }, [router]);
  return <button type="button" onClick={() => router.refresh()}>지금 새로고침</button>;
}
