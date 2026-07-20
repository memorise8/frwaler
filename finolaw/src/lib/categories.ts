// ====================================================================
// Sheet → country/continent/category mapping for libertree.db.
// `sites.sheet` is the upstream classification column. Here we attach
// human-readable country / continent / functional-category labels so
// the dashboard and search filters can group by them.
//
// Coverage: every distinct sheet value currently present in libertree.db
// (see scripts/qa-per-site-crawl.py output) is mapped explicitly.
// New sheets fall back to {country:"기타", continent:"Other",
// category:"기타"} so the UI never crashes on unseen data.
// ====================================================================

export type Continent =
  | "Asia"
  | "Europe"
  | "North America"
  | "South America"
  | "Oceania"
  | "Africa"
  | "Other";

export type SiteFunctionCategory =
  | "Government"
  | "Research"
  | "Academic"
  | "Statistics"
  | "Library"
  | "기타";

export interface SiteCategory {
  sheet: string;
  /** Country/region label (Korean or English, matching the sheet style). */
  country: string;
  /** ISO-3166-1 alpha-2 code where applicable, else "??". */
  countryCode: string;
  continent: Continent;
  category: SiteFunctionCategory;
}

// Explicit map: every sheet listed below was observed in libertree.db.
// Re-run scripts/qa-per-site-crawl.py if new sheets show up; unmapped
// values fall back to UNKNOWN_CATEGORY without breaking the UI.
const SHEET_MAP: Record<string, Omit<SiteCategory, "sheet">> = {
  // -- 한글 ‘완료’ 시리즈 (혼합: 정부/연구/학술) --
  한국완료: { country: "한국", countryCode: "kr", continent: "Asia", category: "기타" },
  "중국 완료": { country: "중국", countryCode: "cn", continent: "Asia", category: "기타" },
  중국완료: { country: "중국", countryCode: "cn", continent: "Asia", category: "기타" },
  일본완료: { country: "일본", countryCode: "jp", continent: "Asia", category: "기타" },
  뉴질랜드완료: { country: "뉴질랜드", countryCode: "nz", continent: "Oceania", category: "기타" },
  호주완료: { country: "호주", countryCode: "au", continent: "Oceania", category: "기타" },
  영국완료: { country: "영국", countryCode: "uk", continent: "Europe", category: "기타" },
  벨기에완료: { country: "벨기에", countryCode: "be", continent: "Europe", category: "기타" },
  "스웨덴 완료": { country: "스웨덴", countryCode: "se", continent: "Europe", category: "기타" },
  스웨덴완료: { country: "스웨덴", countryCode: "se", continent: "Europe", category: "기타" },
  덴마크완료: { country: "덴마크", countryCode: "dk", continent: "Europe", category: "기타" },
  독일완료: { country: "독일", countryCode: "de", continent: "Europe", category: "기타" },
  오스트리아완료: { country: "오스트리아", countryCode: "at", continent: "Europe", category: "기타" },
  핀란드완료: { country: "핀란드", countryCode: "fi", continent: "Europe", category: "기타" },

  // -- North America --
  "American Research Institutes": { country: "미국", countryCode: "us", continent: "North America", category: "Research" },
  "USA Executive Departments": { country: "미국", countryCode: "us", continent: "North America", category: "Government" },
  "Canadian Research Centers": { country: "캐나다", countryCode: "ca", continent: "North America", category: "Research" },
  "Canada Ministries": { country: "캐나다", countryCode: "ca", continent: "North America", category: "Government" },
  "Mexico Ministries": { country: "멕시코", countryCode: "mx", continent: "North America", category: "Government" },

  // -- Europe (분류된 영문 sheet) --
  "Switzerland Research Centers": { country: "스위스", countryCode: "ch", continent: "Europe", category: "Research" },
  "Switzerland Departments": { country: "스위스", countryCode: "ch", continent: "Europe", category: "Government" },
  "French Public Universities": { country: "프랑스", countryCode: "fr", continent: "Europe", category: "Academic" },
  "French Public Institutions": { country: "프랑스", countryCode: "fr", continent: "Europe", category: "Government" },
  "French Research Institutes": { country: "프랑스", countryCode: "fr", continent: "Europe", category: "Research" },
  "France Ministries": { country: "프랑스", countryCode: "fr", continent: "Europe", category: "Government" },
  "Dutch Research Centers": { country: "네덜란드", countryCode: "nl", continent: "Europe", category: "Research" },
  "Irish Research Centers": { country: "아일랜드", countryCode: "ie", continent: "Europe", category: "Research" },
  "Ireland Ministries": { country: "아일랜드", countryCode: "ie", continent: "Europe", category: "Government" },
  "Greece Ministries": { country: "그리스", countryCode: "gr", continent: "Europe", category: "Government" },
  "Greece Research Centers": { country: "그리스", countryCode: "gr", continent: "Europe", category: "Research" },
  "Germany Ministries": { country: "독일", countryCode: "de", continent: "Europe", category: "Government" },
  "GER ResearchCenters(Unfinished)": { country: "독일", countryCode: "de", continent: "Europe", category: "Research" },
  "Italy Ministries": { country: "이탈리아", countryCode: "it", continent: "Europe", category: "Government" },
  "Estonia Research Centers": { country: "에스토니아", countryCode: "ee", continent: "Europe", category: "Research" },
  "Norway Research Centers": { country: "노르웨이", countryCode: "no", continent: "Europe", category: "Research" },
  "Norway Ministries": { country: "노르웨이", countryCode: "no", continent: "Europe", category: "Government" },
  "Slovenia Research Centers": { country: "슬로베니아", countryCode: "si", continent: "Europe", category: "Research" },
  "Spain Ministries": { country: "스페인", countryCode: "es", continent: "Europe", category: "Government" },
  "Spanish Research Centers": { country: "스페인", countryCode: "es", continent: "Europe", category: "Research" },
  "Italy Research centers": { country: "이탈리아", countryCode: "it", continent: "Europe", category: "Research" },
  "Portugal Ministries": { country: "포르투갈", countryCode: "pt", continent: "Europe", category: "Government" },
  "Netherland Ministries": { country: "네덜란드", countryCode: "nl", continent: "Europe", category: "Government" },
  "Polish Research Centers": { country: "폴란드", countryCode: "pl", continent: "Europe", category: "Research" },
  "Estonia Ministries": { country: "에스토니아", countryCode: "ee", continent: "Europe", category: "Government" },

  // -- South America --
  "Chile Ministries": { country: "칠레", countryCode: "cl", continent: "South America", category: "Government" },
  "Chile Research Centers": { country: "칠레", countryCode: "cl", continent: "South America", category: "Research" },
};

const UNKNOWN_CATEGORY: Omit<SiteCategory, "sheet"> = {
  country: "기타",
  countryCode: "??",
  continent: "Other",
  category: "기타",
};

/**
 * Resolve a sheet label to its {country, continent, category} record.
 * Returns the UNKNOWN sentinel for null / unmapped sheets.
 */
export function getCategoryForSheet(sheet: string | null | undefined): SiteCategory {
  if (!sheet) {
    return { sheet: "(없음)", ...UNKNOWN_CATEGORY };
  }
  const hit = SHEET_MAP[sheet];
  if (hit) return { sheet, ...hit };
  // try a trimmed lookup (some sheets contain stray spaces)
  const trimmed = sheet.trim().replace(/\s+/g, " ");
  if (trimmed !== sheet && SHEET_MAP[trimmed]) {
    return { sheet, ...SHEET_MAP[trimmed] };
  }
  return { sheet, ...UNKNOWN_CATEGORY };
}

/** Full ordered list of supported continents — used to build dropdowns. */
export const ALL_CONTINENTS: Continent[] = [
  "Asia",
  "Europe",
  "North America",
  "South America",
  "Oceania",
  "Africa",
  "Other",
];

/** Full ordered list of supported categories — used to build dropdowns. */
export const ALL_CATEGORIES: SiteFunctionCategory[] = [
  "Government",
  "Research",
  "Academic",
  "Statistics",
  "Library",
  "기타",
];

/** Stats helper: classifier metrics so the dashboard can show coverage. */
export function classificationStats(sheets: Iterable<string | null>): {
  total: number;
  mapped: number;
  unmapped: number;
  unmappedSheets: string[];
} {
  const unmappedSet = new Set<string>();
  let total = 0;
  let mapped = 0;
  for (const sheet of sheets) {
    total += 1;
    const key = sheet ?? "";
    if (key && SHEET_MAP[key]) {
      mapped += 1;
      continue;
    }
    const trimmed = key.trim().replace(/\s+/g, " ");
    if (trimmed && SHEET_MAP[trimmed]) {
      mapped += 1;
      continue;
    }
    unmappedSet.add(sheet ?? "(없음)");
  }
  return {
    total,
    mapped,
    unmapped: total - mapped,
    unmappedSheets: Array.from(unmappedSet).sort(),
  };
}
