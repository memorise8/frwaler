import { readFileSync } from "node:fs";
import { getDashboardFixture } from "./fixtures/dashboard-fixture.mjs";
import { getHandoffConfiguration } from "./config/handoff-config.mjs";

const tokenStyles = readFileSync(new URL("./styles/tokens.css", import.meta.url), "utf8");
const baseStyles = readFileSync(new URL("./styles/base.css", import.meta.url), "utf8");
const dashboardStyles = readFileSync(new URL("./styles/dashboard.css", import.meta.url), "utf8");

const escapeHtml = (value) =>
  String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

const renderNavigation = (fixture) =>
  `<nav class="section-navigation" aria-label="${escapeHtml(fixture.navigationLabel)}">
    <ul class="section-navigation__list">
      ${fixture.navigation
        .map(
          ({ label, target }) =>
            `<li><a href="#${escapeHtml(target)}">${escapeHtml(label)}</a></li>`
        )
        .join("")}
    </ul>
  </nav>`;

const renderCoverage = (fixture) => {
  if (fixture.coverage.length === 0) {
    return `<p class="state-notice" role="status">${escapeHtml(fixture.stateNotice)}</p>`;
  }

  return `<dl class="coverage-list">
    ${fixture.coverage
      .map(
        ({ label, value, detail, ratio }) =>
          `<div class="coverage-list__item">
            <dt>${escapeHtml(label)}</dt>
            <dd><strong>${escapeHtml(value)}</strong><span>${escapeHtml(detail)}</span><span>수치: ${escapeHtml(ratio)}</span></dd>
          </div>`
      )
      .join("")}
  </dl>`;
};

const renderMaterialOverview = (fixture) => {
  if (fixture.materialOverview === null) {
    return `<p class="state-notice" role="status">${escapeHtml(fixture.stateNotice)}</p>`;
  }

  return `<article class="material-overview">
    <h3>${escapeHtml(fixture.materialOverview.title)}</h3>
    <p>${escapeHtml(fixture.materialOverview.text)}</p>
    <p>${escapeHtml(fixture.materialOverview.metadata)}</p>
  </article>`;
};

const renderProvenance = (fixture) => {
  if (fixture.provenance.length === 0) {
    return `<p class="state-notice" role="status">${escapeHtml(fixture.stateNotice)}</p>`;
  }

  return `<dl class="provenance-list">
    ${fixture.provenance
      .map(
        ({ label, value }) =>
          `<div class="provenance-list__item"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`
      )
      .join("")}
  </dl>`;
};

const renderExceptions = (fixture) => {
  if (fixture.exceptions.length === 0) {
    return `<p class="state-notice" role="status">${escapeHtml(fixture.stateNotice)}</p>`;
  }

  const [priority, subject, status, guidance] = fixture.table.headers;
  return `<p class="table-scroll-hint">표를 좌우로 밀어 전체 항목 보기</p>
  <div class="table-scroll" tabindex="0" role="region" aria-label="우선 확인 항목 표. 좌우로 스크롤할 수 있습니다.">
  <table class="exceptions-table">
    <caption>${escapeHtml(fixture.table.caption)}</caption>
    <thead><tr><th scope="col">${escapeHtml(priority)}</th><th scope="col">${escapeHtml(subject)}</th><th scope="col">${escapeHtml(status)}</th><th scope="col">${escapeHtml(guidance)}</th></tr></thead>
    <tbody>${fixture.exceptions
      .map(
        (exception) =>
          `<tr><th scope="row">${escapeHtml(exception.priority)}</th><td>${escapeHtml(exception.subject)}</td><td>${escapeHtml(exception.status)}</td><td>${escapeHtml(exception.guidance)}</td></tr>`
      )
      .join("")}</tbody>
  </table>
  </div>`;
};

const renderStateNotice = (fixture) =>
  fixture.stateNotice === undefined
    ? ""
    : `<p class="state-notice" role="status">${escapeHtml(fixture.stateNotice)}</p>`;

export const renderDocument = (variant = "default") => {
  const fixture = getDashboardFixture(variant);
  const handoff = getHandoffConfiguration();
  const handoffMarker = handoff.kind === "absent" ? "" : "<!-- future handoff is not rendered in this MVP -->";

  return `<!doctype html>
<html lang="ko">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>${escapeHtml(fixture.dashboardTitle)}</title>
    <style>${tokenStyles}\n${baseStyles}\n${dashboardStyles}</style>
  </head>
  <body>
    <header class="site-header">
      <div class="site-header__inner">
      <p class="data-classification">${escapeHtml(fixture.dataClassification)}</p>
      <h1>${escapeHtml(fixture.dashboardTitle)}</h1>
      ${renderNavigation(fixture)}
      </div>
    </header>
    <main class="dashboard-shell">
      <section class="dashboard-section overview" id="overview" aria-labelledby="overview-heading">
        <h2 id="overview-heading">${escapeHtml(fixture.sectionLabels.overview)}</h2>
        <p><strong>${escapeHtml(fixture.snapshot.label)}</strong>: ${escapeHtml(fixture.snapshot.value)}</p>
        <p>${escapeHtml(fixture.snapshot.context)}</p>
        ${renderStateNotice(fixture)}
      </section>
      <section class="dashboard-section" aria-labelledby="coverage-heading">
        <h2 id="coverage-heading">${escapeHtml(fixture.sectionLabels.coverage)}</h2>
        ${renderCoverage(fixture)}
      </section>
      <section class="dashboard-section" id="material-overview" aria-labelledby="material-overview-heading">
        <h2 id="material-overview-heading">${escapeHtml(fixture.sectionLabels.materialOverview)}</h2>
        ${renderMaterialOverview(fixture)}
      </section>
      <section class="dashboard-section" aria-labelledby="provenance-heading">
        <h2 id="provenance-heading">${escapeHtml(fixture.sectionLabels.provenance)}</h2>
        ${renderProvenance(fixture)}
      </section>
      <section class="dashboard-section" id="exceptions" aria-labelledby="exceptions-heading">
        <h2 id="exceptions-heading">${escapeHtml(fixture.sectionLabels.exceptions)}</h2>
        ${renderExceptions(fixture)}
      </section>
    </main>
    ${handoffMarker}
  </body>
</html>`;
};
