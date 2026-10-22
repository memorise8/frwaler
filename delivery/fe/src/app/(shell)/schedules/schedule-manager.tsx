"use client";

import { FormEvent, useState } from "react";
import { parseScheduleLimit } from "@/lib/crawl-limit";
import { formatSeoul, SEOUL_LABEL } from "@/lib/seoul-time";
import { formatScheduleInterval, formatScheduleMode, isScheduleDirty, pendingScheduleSaves, SCHEDULE_INTERVAL_OPTIONS, setScheduleDraft, shownEnabled, type ScheduleDrafts } from "@/lib/schedule-display";
import { describeScheduleSaveFailure, SCHEDULE_SAVE_NETWORK_ERROR_MESSAGE, type BackendErrorBody } from "@/lib/schedule-save-error";

type Schedule = { id: number; site_id: string; interval_hours: number; mode: string; limit_n: number | null; enabled: boolean; next_run_at: string; last_run_at: string | null };
type SiteOption = { siteId: string; siteName: string };

export default function ScheduleManager({ initialRows, sites }: { initialRows: Schedule[]; sites: SiteOption[] }) {
  const [trial, setTrial] = useState(false);
  const [rows, setRows] = useState(initialRows);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  // 사용 체크박스의 저장 전 값. 체크만 바꾸면 이 줄이 "저장 필요"가 되고, 변경 저장 버튼이 보낸다.
  const [drafts, setDrafts] = useState<ScheduleDrafts>({});
  const [saving, setSaving] = useState(false);

  const save = async (siteId: string, body: object): Promise<boolean> => {
    setMessage(null);
    if ("enabled" in body && body.enabled && !trial) { setMessage({text: "미검증 수집기의 시험 예약 실행을 확인해 주세요.", error: true}); return false; }
    let response: Response;
    try {
      response = await fetch(`/api/schedules/${encodeURIComponent(siteId)}`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({...body, trial}),
      });
    } catch {
      setMessage({ text: SCHEDULE_SAVE_NETWORK_ERROR_MESSAGE, error: true });
      return false;
    }
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      setMessage({ text: describeScheduleSaveFailure(response.status, {}), error: true });
      return false;
    }
    if (!response.ok) {
      setMessage({ text: describeScheduleSaveFailure(response.status, payload as BackendErrorBody), error: true });
      return false;
    }
    const row = payload as Schedule;
    setRows((old) => [...old.filter((item) => item.site_id !== row.site_id), row].sort((a, b) => a.site_id.localeCompare(b.site_id)));
    setDrafts((old) => setScheduleDraft(old, row, row.enabled));
    setMessage({ text: `${siteId} 예약을 저장했습니다.`, error: false });
    return true;
  };

  const pending = pendingScheduleSaves(rows, drafts);

  // 저장 전 체크 변경을 한 번에 보낸다. 실패하면 그 줄에서 멈춰 오류를 그대로 보여 준다.
  const saveChanges = async () => {
    setSaving(true);
    let saved = 0;
    for (const change of pending) {
      if (!(await save(change.siteId, change.body))) break;
      saved += 1;
    }
    setSaving(false);
    if (saved > 1 && saved === pending.length) setMessage({ text: `예약 ${saved}건의 변경을 저장했습니다.`, error: false });
  };

  // 두 단계로 나눈 이유: 예약 목록은 한 화면에 여러 줄이고, 되돌리기가 없다.
  const [confirming, setConfirming] = useState<string | null>(null);

  const remove = async (siteId: string) => {
    setMessage(null);
    let response: Response;
    try {
      response = await fetch(`/api/schedules/${encodeURIComponent(siteId)}`, { method: "DELETE" });
    } catch {
      setMessage({ text: SCHEDULE_SAVE_NETWORK_ERROR_MESSAGE, error: true });
      return;
    }
    // A 404 here means someone else (another tab, another operator) already
    // deleted this schedule. That is not a failure from this operator's
    // point of view -- the row they wanted gone is gone -- so it is treated
    // as success rather than left stuck in the table with a generic error.
    if (response.status === 404) {
      setRows((old) => old.filter((item) => item.site_id !== siteId));
      setConfirming(null);
      setMessage({ text: `${siteId} 예약은 이미 삭제되어 있었습니다.`, error: false });
      return;
    }
    if (!response.ok) {
      setMessage({ text: `${siteId} 예약을 삭제하지 못했습니다.`, error: true });
      return;
    }
    setRows((old) => old.filter((item) => item.site_id !== siteId));
    setConfirming(null);
    setMessage({ text: `${siteId} 예약을 삭제했습니다. 이미 등록된 작업은 그대로 진행됩니다.`, error: false });
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const siteId = String(data.get("site_id") ?? "").trim();
    if (!siteId) return;
    const limitResult = parseScheduleLimit(data.get("limit_n"));
    if (!limitResult.ok) {
      setMessage({ text: limitResult.error, error: true });
      return;
    }
    // 저장에 실패하면 입력을 지우지 않는다(다시 입력하지 않고 고쳐서 재시도할 수 있게).
    if (await save(siteId, { interval_hours: Number(data.get("interval_hours")), mode: "incremental", limit_n: limitResult.limit, enabled: true })) form.reset();
  };

  return (
    <>
      <label className="run-confirm"><input type="checkbox" checked={trial} onChange={event => setTrial(event.target.checked)} />수집 순서와 이어받기가 미검증인 시험 예약임을 확인합니다.</label>
      <form className="document-filters" onSubmit={(event) => void submit(event)}>
        <label><span>사이트 ID</span><input name="site_id" required placeholder="예: government-se-publications" list="site-options" autoComplete="off" /></label>
        <label><span>수집 주기</span><select name="interval_hours" defaultValue="168">{SCHEDULE_INTERVAL_OPTIONS.map((option) => <option key={option.hours} value={option.hours}>{option.label}</option>)}</select></label>
        <label><span>1회 수집 제한</span><input name="limit_n" type="number" min="1" max="1000" defaultValue="100" placeholder="예: 100" /></label>
        <button type="submit">예약 저장</button>
      </form>
      <datalist id="site-options">
        {sites.map((site) => <option key={site.siteId} value={site.siteId}>{site.siteName || site.siteId}</option>)}
      </datalist>
      {message && <p className={`run-message${message.error ? " run-message--error" : " run-message--success"}`} role={message.error ? "alert" : undefined}>{message.text}</p>}
      {rows.length ? (
        <>
        <div className="run-confirm">
          <button type="button" disabled={!pending.length || saving} onClick={() => void saveChanges()}>
            {saving ? "저장 중…" : pending.length ? `변경 저장 (${pending.length}건)` : "변경 저장"}
          </button>
          <span>{pending.length ? "사용 체크를 바꾼 예약이 아직 저장되지 않았습니다." : "사용 체크를 바꾸면 여기서 저장합니다."}</span>
        </div>
        <div className="table-shell">
          <table>
            <thead>
              <tr><th>사이트</th><th>주기</th><th>방식·1회 제한</th><th>사용</th><th>다음 실행({SEOUL_LABEL})</th><th>최근 실행</th><th>삭제</th></tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td><code>{row.site_id}</code></td>
                  <td>{formatScheduleInterval(row.interval_hours)}</td>
                  <td>{formatScheduleMode(row.mode)} · {row.limit_n === null ? "제한 없음" : `${row.limit_n.toLocaleString("ko-KR")}건`}</td>
                  <td>
                    <label title="체크를 바꾼 뒤 위의 변경 저장을 눌러야 반영됩니다.">
                      <input
                        type="checkbox"
                        checked={shownEnabled(drafts, row)}
                        onChange={(event) => setDrafts((old) => setScheduleDraft(old, row, event.target.checked))}
                      />
                      {shownEnabled(drafts, row) ? " 사용" : " 중지"}
                    </label>
                    {isScheduleDirty(drafts, row) && <small className="run-message--error"> · 저장 필요</small>}
                  </td>
                  <td>{formatSeoul(row.next_run_at, { fallback: "알 수 없음" })}</td>
                  <td>{row.last_run_at ? formatSeoul(row.last_run_at, { fallback: "알 수 없음" }) : "아직 없음"}</td>
                  <td>
                    {confirming === row.site_id ? (
                      <>
                        <button type="button" onClick={() => void remove(row.site_id)}>삭제 확인</button>
                        <button type="button" onClick={() => setConfirming(null)}>취소</button>
                      </>
                    ) : (
                      <button type="button" onClick={() => setConfirming(row.site_id)}>삭제</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="run-note">필요 없는 예약은 사용 체크를 끄고 변경 저장을 누르거나 삭제할 수 있습니다. 중지된 예약은 목록에 남아 있으며, 다시 체크하고 저장하기 전까지 실행되지 않습니다. 삭제해도 이미 등록된 작업은 그대로 진행되며 되돌릴 수 없습니다.</p>
        </div>
        </>
      ) : (
        <div className="document-empty">
          <strong>등록된 예약이 없습니다.</strong>
          <p>필요한 사이트만 새 글을 주기적으로 모으도록(증분) 설정할 수 있습니다.</p>
        </div>
      )}
    </>
  );
}
