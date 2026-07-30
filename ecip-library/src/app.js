import { catalogue } from "./catalogue.js";

const books = catalogue;
const app = document.querySelector("#app");

if (app === null) {
  throw new Error("앱 컨테이너를 찾을 수 없습니다.");
}

const escapeHtml = (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const displayValue = (value) => value === null || value === undefined || value.trim() === "" ? "정보 없음" : escapeHtml(value);
const readingText = (value) => escapeHtml(value)
  .replaceAll("경제 성장을", '<span class="keep-phrase">경제 성장을</span>')
  .replaceAll("산업 전략", '<span class="keep-phrase">산업 전략</span>')
  .replaceAll("재정 규율", '<span class="keep-phrase">재정 규율</span>');
const sourceOf = (book) => book.sourceName ?? book.sourceHost ?? "출처 정보 없음";
const sourceHref = (source) => `#/source/${encodeURIComponent(source)}`;
const catalogueHref = (source) => source === null ? "#/catalogue" : sourceHref(source);
const decodeRouteSegment = (value) => {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
};
const sources = [...new Map(books.map((book) => [sourceOf(book), 0])).keys()]
  .map((source) => ({ source, count: books.filter((book) => sourceOf(book) === source).length }))
  .sort((left, right) => left.source.localeCompare(right.source, "ko-KR"));

const introductionSource = (book) => {
  if (book.introductionLabel === "요약") return "저장된 한국어 요약";
  if (book.introductionLabel === "도서 소개") return "초록 기반 도서 소개";
  return "소개 정보 없음";
};

const formatDate = (value) => {
  if (value === null || value === undefined) return "발행일 정보 없음";
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(parsed.getTime()) ? escapeHtml(value) : new Intl.DateTimeFormat("ko-KR", { dateStyle: "long", timeZone: "UTC" }).format(parsed);
};

const metadata = (book) => [["저자", book.authors], ["발행처", book.publisher], ["학술지·시리즈", book.journal], ["발행일", book.publishedDate === null ? null : formatDate(book.publishedDate)], ["원문 출처", sourceOf(book)]];

const masthead = () => `
  <header class="masthead"><div class="shell masthead__inner">
    <a class="wordmark" href="#/catalogue" aria-label="E-CIP 연구자료 도서관 첫 화면">E-CIP <span>연구자료 도서관</span></a>
    <p class="preview-note" role="status">로컬 검토용·공개 배포 전</p>
  </div></header>`;

const emptyResult = (message, action, href = "#/catalogue", actionId = "") => `
  <section class="empty-state" aria-live="polite"><p class="eyebrow">CATALOGUE NOTE</p><h2>${message}</h2>${actionId === "" ? `<a class="text-action" href="${href}">${action}</a>` : `<button class="text-action" type="button" id="${actionId}">${action}</button>`}</section>`;

const bookSlip = (book, selectedSource) => {
  const source = selectedSource === null ? "" : `?source=${encodeURIComponent(selectedSource)}`;
  return `<article class="book-slip"><div class="book-slip__spine" aria-hidden="true"></div><div class="book-slip__body">
    <p class="book-slip__kicker">${introductionSource(book)}</p><h2><a href="#/book/${encodeURIComponent(book.publicId)}${source}">${escapeHtml(book.title)}</a></h2>
    <p class="book-slip__intro">${book.introduction === null ? "이 자료에는 공개 가능한 소개 문구가 아직 없습니다." : readingText(book.introduction)}</p>
    <dl class="book-slip__meta"><div><dt>저자</dt><dd>${displayValue(book.authors)}</dd></div><div><dt>발행처</dt><dd>${displayValue(book.publisher)}</dd></div></dl>
  </div><time class="book-slip__date" datetime="${book.publishedDate ?? ""}">${formatDate(book.publishedDate)}</time></article>`;
};

const sourceBrowser = (selectedSource) => `
  <nav class="source-browser" aria-labelledby="source-browser-heading">
    <div class="source-browser__heading"><div><p class="eyebrow">SOURCE INDEX</p><h2 id="source-browser-heading">출처별 서가</h2></div><a class="source-reset" href="#/catalogue">모든 출처 보기</a></div>
    ${selectedSource === null ? "" : `<p class="active-source" aria-live="polite">현재 출처 <strong>${escapeHtml(selectedSource)}</strong>의 자료를 보고 있습니다.</p>`}
    <ul>${sources.map(({ source, count }) => `<li><a href="${sourceHref(source)}"${source === selectedSource ? ' aria-current="page"' : ""}><span>${escapeHtml(source)}</span><b>${count}권</b></a></li>`).join("")}</ul>
  </nav>`;

const catalogueView = (query = "", selectedSource = null) => {
  const normalizedQuery = query.trim().toLocaleLowerCase("ko-KR");
  const sourceBooks = selectedSource === null ? books : books.filter((book) => sourceOf(book) === selectedSource);
  const matches = normalizedQuery === "" ? sourceBooks : sourceBooks.filter((book) => [book.title, book.authors, book.publisher, book.journal, book.keywords, book.introduction].filter((value) => value !== null && value !== undefined).join(" ").toLocaleLowerCase("ko-KR").includes(normalizedQuery));
  const resultLabel = normalizedQuery === "" ? `${selectedSource === null ? "전체" : "출처 자료"} ${matches.length}권` : `검색 결과 ${matches.length}권`;
  return `${masthead()}<main class="shell catalogue" id="main-content">
    <section class="catalogue__intro" aria-labelledby="catalogue-title"><p class="eyebrow">LOCAL RESEARCH SHELF</p><h1 id="catalogue-title">연구자료를<br /><em>천천히</em> 읽는 서가</h1><p>검토를 위해 선별된 자료의 서지와 소개를 한곳에 모았습니다. 원문은 각 자료의 출처에서 <span class="keep-phrase">확인할 수 있습니다.</span></p></section>
    ${sourceBrowser(selectedSource)}
    <section class="catalogue__tools" aria-label="자료 검색"><label for="catalogue-search">${selectedSource === null ? "이 서가에서 찾기" : `${escapeHtml(selectedSource)} 안에서 찾기`}</label><div class="search-row"><input id="catalogue-search" type="search" autocomplete="off" placeholder="제목, 저자, 발행처, 주제로 검색" value="${escapeHtml(query)}" /><button type="button" id="clear-search" ${query === "" ? "hidden" : ""}>지우기</button></div><p id="result-count" aria-live="polite">${resultLabel}</p></section>
    <section class="catalogue__results" aria-label="도서 목록">${books.length === 0 ? emptyResult("아직 서가에 담긴 자료가 없습니다.", "목록으로 돌아가기") : matches.length === 0 ? emptyResult("찾으신 자료가 이 출처 서가에는 없습니다.", "검색어 지우기", "", "clear-empty-search") : matches.map((book) => bookSlip(book, selectedSource)).join("")}</section>
  </main>`;
};

const externalLink = (href, label) => href === null || href === undefined ? "" : `<a class="source-link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer"><span>${label}</span><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h9M8.5 3.5 13 8l-4.5 4.5" /></svg></a>`;

const detailView = (book, selectedSource) => {
  if (book === undefined) return `${masthead()}<main class="shell detail" id="main-content">${emptyResult("요청하신 자료를 찾지 못했습니다.", "서가로 돌아가기", catalogueHref(selectedSource))}</main>`;
  const details = metadata(book).map(([label, value]) => `<div><dt>${label}</dt><dd>${displayValue(value)}</dd></div>`).join("");
  const keywords = book.keywords === null || book.keywords.trim() === "" ? "<p class=\"muted-copy\">등록된 주제어가 없습니다.</p>" : `<ul class="keyword-list">${book.keywords.split(",").map((keyword) => `<li>${escapeHtml(keyword.trim())}</li>`).join("")}</ul>`;
  const introduction = book.introduction === null ? "<p class=\"unavailable\">이 자료에는 공개 가능한 소개 문구가 아직 없습니다.</p>" : `<p>${readingText(book.introduction)}</p>`;
  return `${masthead()}<main class="shell detail" id="main-content"><a class="back-link" href="${catalogueHref(selectedSource)}">← ${selectedSource === null ? "서가로" : "선택한 출처 서가로"} 돌아가기</a><article class="book-record"><header class="book-record__header"><p class="eyebrow">${introductionSource(book)}</p><h1>${escapeHtml(book.title)}</h1><p class="book-record__author">${displayValue(book.authors)}</p></header><div class="book-record__grid"><aside class="metadata-ledger" aria-label="서지 정보"><dl>${details}</dl></aside><div class="reading-copy"><section aria-labelledby="introduction-heading"><p class="eyebrow">READING NOTE</p><h2 id="introduction-heading">도서 소개</h2><p class="source-label">${introductionSource(book)}</p>${introduction}</section><section aria-labelledby="keywords-heading"><h2 id="keywords-heading">주제어</h2>${keywords}</section><section aria-labelledby="source-heading"><h2 id="source-heading">원문 출처</h2><div class="source-links">${externalLink(book.originalUrl, "원문 안내 페이지 열기")}${externalLink(book.pdfUrl, "원문 PDF 열기")}${book.originalUrl === null && book.pdfUrl === null ? "<p class=\"muted-copy\">등록된 원문 링크가 없습니다.</p>" : ""}</div></section></div></div></article></main>`;
};

const currentRoute = () => window.location.hash.replace(/^#/, "") || "/catalogue";
const render = () => {
  const route = currentRoute();
  const [path, search] = route.split("?");
  const sourceMatch = /^\/source\/([^/]+)$/.exec(path);
  const bookMatch = /^\/book\/([^/]+)$/.exec(path);
  const requestedSource = sourceMatch === null ? null : decodeRouteSegment(sourceMatch[1] ?? "");
  const detailSource = new URLSearchParams(search).get("source");
  const selectedSource = sources.some(({ source }) => source === (sourceMatch === null ? detailSource : requestedSource)) ? (sourceMatch === null ? detailSource : requestedSource) : null;
  app.innerHTML = bookMatch === null ? catalogueView("", selectedSource) : detailView(books.find((book) => book.publicId === decodeRouteSegment(bookMatch[1] ?? "")), selectedSource);
  if (bookMatch === null) bindSearch(selectedSource);
};
const bindSearch = (selectedSource) => {
  const input = document.querySelector("#catalogue-search");
  const clear = document.querySelector("#clear-search");
  const emptyClear = document.querySelector("#clear-empty-search");
  if (input === null || clear === null) return;
  input.addEventListener("input", () => { const start = input.selectionStart; app.innerHTML = catalogueView(input.value, selectedSource); const replacement = document.querySelector("#catalogue-search"); if (replacement !== null) { replacement.focus(); replacement.setSelectionRange(start, start); } bindSearch(selectedSource); });
  clear.addEventListener("click", () => { app.innerHTML = catalogueView("", selectedSource); const replacement = document.querySelector("#catalogue-search"); replacement?.focus(); bindSearch(selectedSource); });
  emptyClear?.addEventListener("click", () => { app.innerHTML = catalogueView("", selectedSource); const replacement = document.querySelector("#catalogue-search"); replacement?.focus(); bindSearch(selectedSource); });
};
window.addEventListener("hashchange", render);
render();
