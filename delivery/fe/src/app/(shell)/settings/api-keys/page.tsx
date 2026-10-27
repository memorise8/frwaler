import "server-only";
import ApiKeysManager from "./api-keys-manager";
import { operatorHeaders } from "@/lib/backend-auth";
import type { ApiKeyList } from "@/lib/api-keys";

// 서버 데이터를 읽는 다른 화면과 같이 빌드 때 미리 그리지 않는다(빌드 중에는 BE 가 없다).
export const dynamic = "force-dynamic";

const backend = () => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

async function load(): Promise<ApiKeyList | null> {
  try {
    const r = await fetch(`${backend()}/settings/api-keys`, { headers: operatorHeaders(), cache: "no-store", signal: AbortSignal.timeout(8000) });
    if (!r.ok) return null;
    return await r.json() as ApiKeyList;
  } catch {
    return null;
  }
}

export default async function ApiKeysPage() {
  const list = await load();
  return <div className="documents-page"><header className="documents-hero"><div><p className="eyebrow">SETTINGS</p><h1>API 키</h1></div><p>공급자 API 키를 넣으면 그 키로 공급자에 가벼운 요청을 한 번 보내 시험하고, 정상일 때만 저장합니다. 저장한 키는 다시 보여 주지 않습니다.</p></header>{list === null ? <aside className="snapshot-note snapshot-note--unavailable"><strong>연결 안 됨</strong><span>API 키 상태를 불러올 수 없습니다.</span></aside> : <ApiKeysManager list={list} />}</div>;
}
