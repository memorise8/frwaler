"use client";

import { useState } from "react";
import { facetCountLabel, type Facet } from "@/lib/facet-options";
import { searchDisabledReason } from "@/lib/document-paging";
import styles from "./documents.module.css";

// The BE answers q without site_id with 422 q_requires_site, so the search box
// only unlocks once a site is picked here (no round trip needed).
export default function SearchFields({ sites, siteId, q }: Readonly<{
  sites: readonly Facet[]; siteId: string; q: string;
}>) {
  const [site, setSite] = useState(siteId);
  const reason = searchDisabledReason(site);
  return <>
    <label className={styles.site}><span>사이트</span>
      <select name="site_id" value={site} onChange={(event) => setSite(event.target.value)}>
        <option value="">전체 사이트</option>
        {sites.map((item) => <option key={item.value} value={item.value}>{item.label || item.value}{facetCountLabel(item)}</option>)}
      </select>
    </label>
    <label className={styles.query}><span>검색어</span>
      <input name="q" defaultValue={q} disabled={reason !== null} placeholder={reason ?? "제목, 초록, 키워드"}
        aria-describedby={reason ? "document-q-reason" : undefined} />
      {reason && <small id="document-q-reason" className={styles.reason}>{reason}</small>}
    </label>
  </>;
}
