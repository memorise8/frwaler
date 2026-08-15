export type Facet = Readonly<{ value: string; count: number; label?: string }>;

// -1 marks a facet option this module synthesized to keep the operator's
// selection visible when its real count isn't known -- either because the
// facet list failed to load at all, or because the value simply isn't in
// whatever facet list did load. A facet actually served by the BE should
// never carry a negative count, so this doubles as an unambiguous marker
// callers can use to render it differently (see facetOptionsWithSelection).
const RETAINED_COUNT = -1;

export const isRetainedFacetOption = (facet: Facet): boolean => facet.count === RETAINED_COUNT;

// The document search form's country/doc_type/site/lang <select> filters
// are populated from the catalogue's facet lists. When that fetch fails,
// `facets` is null and every <select> renders with only its "전체" option
// -- but the operator's current choice is still sitting in the URL and in
// `selected`. Because it has no matching <option>, the browser silently
// shows the select as blank, and pressing 검색 resubmits that blank value
// in place of what the operator actually asked for. The same problem
// exists any time a selected value simply isn't part of the loaded facet
// list, outage or not.
//
// Ensuring the selected value always has a matching <option> -- synthesized
// when it's missing -- means the browser always has something to select,
// so the submitted form can never silently diverge from what is shown.
export const facetOptionsWithSelection = (
  facets: readonly Facet[] | null | undefined,
  selected: string,
): readonly Facet[] => {
  const list = facets ?? [];
  const value = selected.trim();
  if (!value || list.some((item) => item.value === value)) return list;
  return [{ value, count: RETAINED_COUNT }, ...list];
};
