"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { formatSeoul, SEOUL_LABEL } from "@/lib/seoul-time";
import { API_KEY_MAX_LENGTH, API_KEY_NETWORK_ERROR, configuredLabel, describeApiKeyFailure, lastTestLabel, usable, type ApiKeyList, type ApiKeyRow } from "@/lib/api-keys";

type Message = { text: string; error: boolean };

function KeyForm({ row, onDone }: { row: ApiKeyRow; onDone: (message: Message) => void }) {
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const input = form.elements.namedItem("key") as HTMLInputElement;
    const key = input.value.trim();
    if (!key) return;
    setBusy(true);
    let message: Message;
    try {
      const response = await fetch(`/api/settings/api-keys/${encodeURIComponent(row.provider)}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const payload = await response.json().catch(() => ({}));
      message = response.ok
        ? { text: `${row.label} 키를 시험했고 정상이라 저장했습니다. 이제 사용할 수 있습니다.`, error: false }
        : { text: `${row.label}: ${describeApiKeyFailure(response.status, payload)}`, error: true };
    } catch {
      message = { text: `${row.label}: ${API_KEY_NETWORK_ERROR}`, error: true };
    }
    // 결과가 나오면 성공·실패 모두 입력칸을 비운다(키가 화면에 남지 않게).
    input.value = "";
    setBusy(false);
    onDone(message);
  };

  if (!row.testable) return <span className="run-note">시험 방법 준비 중이라 아직 저장할 수 없습니다.</span>;
  return (
    <form className="document-filters" onSubmit={(event) => void submit(event)}>
      <label><span>새 키</span><input name="key" type="password" autoComplete="off" spellCheck={false} maxLength={API_KEY_MAX_LENGTH} required disabled={busy} /></label>
      <button type="submit" disabled={busy}>{busy ? "시험 중…" : "시험 후 저장"}</button>
    </form>
  );
}

export default function ApiKeysManager({ list }: { list: ApiKeyList }) {
  const router = useRouter();
  const [message, setMessage] = useState<Message | null>(null);

  const done = (next: Message) => {
    setMessage(next);
    router.refresh();
  };

  return (
    <>
      {!list.available && <aside className="snapshot-note snapshot-note--unavailable"><strong>보관 폴더 없음</strong><span>서버에 키 보관 폴더가 설정되지 않아 키를 저장할 수 없습니다. 관리자에게 문의해 주세요.</span></aside>}
      {message && <p className={`run-message${message.error ? " run-message--error" : " run-message--success"}`} role={message.error ? "alert" : undefined}>{message.text}</p>}
      <div className="table-shell">
        <table>
          <thead>
            <tr><th>공급자</th><th>상태</th><th>마지막 시험</th><th>시험 시각({SEOUL_LABEL})</th><th>시험 방법</th><th>키 넣기</th></tr>
          </thead>
          <tbody>
            {list.providers.map((row) => (
              <tr key={row.provider}>
                <td>{row.label}</td>
                <td>{configuredLabel(row)}{usable(row) ? " · 사용 가능" : ""}</td>
                <td>{lastTestLabel(row)}</td>
                <td>{row.last_test ? formatSeoul(row.last_test.at, { fallback: "알 수 없음" }) : "-"}</td>
                <td>{row.test_method}</td>
                <td>{list.available ? <KeyForm row={row} onDone={done} /> : "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
