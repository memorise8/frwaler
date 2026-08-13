"use client";

export default function Error({ error, reset }: Readonly<{ error: Error & { digest?: string }; reset: () => void }>) {
  return (
    <div className="detail-unavailable">
      <p className="eyebrow">오류 발생</p>
      <h1>문제가 발생했습니다.</h1>
      <p>페이지를 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.</p>
      {error.digest ? <p>오류 코드: <code>{error.digest}</code></p> : null}
      <button type="button" onClick={reset}>다시 시도</button>
    </div>
  );
}
