export const CONTINENTS = ["Asia", "Europe", "North America", "South America", "Oceania", "Africa", "Other"] as const
export type Continent = (typeof CONTINENTS)[number]
export const SITE_CATEGORIES = ["Government", "Research", "Academic", "Statistics", "Library", "기타"] as const
export type SiteCategory = (typeof SITE_CATEGORIES)[number]

const CONTINENT_LABELS: Readonly<Record<Continent, string>> = {
  Africa: "아프리카",
  Asia: "아시아",
  Europe: "유럽",
  "North America": "북아메리카",
  Oceania: "오세아니아",
  Other: "기타 지역",
  "South America": "남아메리카",
}

const SITE_CATEGORY_LABELS: Readonly<Record<SiteCategory, string>> = {
  Academic: "학술기관",
  Government: "정부기관",
  Library: "도서관",
  Research: "연구기관",
  Statistics: "통계기관",
  기타: "기타 기관",
}

export const continentLabel = (continent: Continent): string => CONTINENT_LABELS[continent]
export const siteCategoryLabel = (category: SiteCategory): string => SITE_CATEGORY_LABELS[category]

type Taxonomy = { readonly category: SiteCategory; readonly continent: Continent; readonly country: string; readonly sheet: string }

const TAXONOMY: Readonly<Record<string, Omit<Taxonomy, "sheet">>> = {
  "American Research Institutes": { country: "미국", continent: "North America", category: "Research" },
  "USA Executive Departments": { country: "미국", continent: "North America", category: "Government" },
  "Canadian Research Centers": { country: "캐나다", continent: "North America", category: "Research" },
  "Canada Ministries": { country: "캐나다", continent: "North America", category: "Government" },
  "Mexico Ministries": { country: "멕시코", continent: "North America", category: "Government" },
  "French Public Universities": { country: "프랑스", continent: "Europe", category: "Academic" },
  "French Public Institutions": { country: "프랑스", continent: "Europe", category: "Government" },
  "French Research Institutes": { country: "프랑스", continent: "Europe", category: "Research" },
  "France Ministries": { country: "프랑스", continent: "Europe", category: "Government" },
  "Germany Ministries": { country: "독일", continent: "Europe", category: "Government" },
  "Italy Ministries": { country: "이탈리아", continent: "Europe", category: "Government" },
  "Portugal Ministries": { country: "포르투갈", continent: "Europe", category: "Government" },
  "Chile Ministries": { country: "칠레", continent: "South America", category: "Government" },
  "Chile Research Centers": { country: "칠레", continent: "South America", category: "Research" },
  "Dutch Research Centers": { country: "네덜란드", continent: "Europe", category: "Research" },
  "Estonia Ministries": { country: "에스토니아", continent: "Europe", category: "Government" },
  "Estonia Research Centers": { country: "에스토니아", continent: "Europe", category: "Research" },
  "GER ResearchCenters(Unfinished)": { country: "독일", continent: "Europe", category: "Research" },
  "Greece Ministries": { country: "그리스", continent: "Europe", category: "Government" },
  "Greece Research Centers": { country: "그리스", continent: "Europe", category: "Research" },
  "Ireland Ministries": { country: "아일랜드", continent: "Europe", category: "Government" },
  "Irish Research Centers": { country: "아일랜드", continent: "Europe", category: "Research" },
  "Italy Research centers": { country: "이탈리아", continent: "Europe", category: "Academic" },
  "Netherland Ministries": { country: "네덜란드", continent: "Europe", category: "Government" },
  "Norway Ministries": { country: "노르웨이", continent: "Europe", category: "Government" },
  "Norway Research Centers": { country: "노르웨이", continent: "Europe", category: "Research" },
  "Polish Research Centers": { country: "폴란드", continent: "Europe", category: "Research" },
  "Slovenia Research Centers": { country: "슬로베니아", continent: "Europe", category: "Research" },
  "Spain Ministries": { country: "스페인", continent: "Europe", category: "Government" },
  "Spanish Research Centers": { country: "스페인", continent: "Europe", category: "Research" },
  "Switzerland Departments": { country: "스위스", continent: "Europe", category: "Government" },
  "Switzerland Research Centers": { country: "스위스", continent: "Europe", category: "Research" },
  한국완료: { country: "한국", continent: "Asia", category: "기타" },
  "중국 완료": { country: "중국", continent: "Asia", category: "기타" },
  중국완료: { country: "중국", continent: "Asia", category: "기타" },
  일본완료: { country: "일본", continent: "Asia", category: "기타" },
  호주완료: { country: "호주", continent: "Oceania", category: "기타" },
  영국완료: { country: "영국", continent: "Europe", category: "기타" },
  뉴질랜드완료: { country: "뉴질랜드", continent: "Oceania", category: "Government" },
  덴마크완료: { country: "덴마크", continent: "Europe", category: "Government" },
  독일완료: { country: "독일", continent: "Europe", category: "Government" },
  벨기에완료: { country: "벨기에", continent: "Europe", category: "기타" },
  "스웨덴 완료": { country: "스웨덴", continent: "Europe", category: "기타" },
  오스트리아완료: { country: "오스트리아", continent: "Europe", category: "Government" },
  핀란드완료: { country: "핀란드", continent: "Europe", category: "Government" },
} as const

export const getCategoryForSheet = (sheet: string | null): Taxonomy => {
  const normalized = sheet?.trim().replace(/\s+/g, " ") ?? "(없음)"
  const matched = TAXONOMY[normalized]
  return matched === undefined
    ? { sheet: normalized, country: "기타", continent: "Other", category: "기타" }
    : { sheet: normalized, ...matched }
}
