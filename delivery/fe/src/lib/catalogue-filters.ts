export type CatalogueFilters = Readonly<{
  query: string;
  status: string;
  category: string;
  country: string;
  docType: string;
  freshness: string;
}>;

const isActive = (value: string): boolean => value.trim() !== "";

// The crawler catalogue table only renders once the visitor has expressed
// some intent to narrow it down — otherwise it would dump the entire
// registry onto the page. Any one of the filter form's fields counts as
// that intent, not just country/docType. freshness is included so a click
// on one of the "사이트 최신화" cards reveals the table on its own, without
// also requiring a status/country/docType selection.
export const hasActiveCatalogueFilter = (filters: CatalogueFilters): boolean =>
  isActive(filters.query) ||
  isActive(filters.status) ||
  isActive(filters.category) ||
  isActive(filters.country) ||
  isActive(filters.docType) ||
  isActive(filters.freshness);
