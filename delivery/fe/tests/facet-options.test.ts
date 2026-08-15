import { describe, expect, it } from "vitest";
import { facetOptionsWithSelection, isRetainedFacetOption } from "../src/lib/facet-options";

const FACETS = [
  { value: "대한민국", count: 120 },
  { value: "호주", count: 45 },
];

describe("facetOptionsWithSelection", () => {
  it("returns the facet list unchanged when nothing is selected", () => {
    expect(facetOptionsWithSelection(FACETS, "")).toEqual(FACETS);
  });

  it("returns the facet list unchanged when the selection is already present", () => {
    expect(facetOptionsWithSelection(FACETS, "호주")).toEqual(FACETS);
  });

  it("prepends a retained option when the selection is missing from a loaded list", () => {
    const result = facetOptionsWithSelection(FACETS, "미국");
    expect(result[0]).toEqual({ value: "미국", count: -1 });
    expect(result.slice(1)).toEqual(FACETS);
  });

  it("synthesizes a single retained option when facets is null (fetch failure)", () => {
    const result = facetOptionsWithSelection(null, "대한민국");
    expect(result).toEqual([{ value: "대한민국", count: -1 }]);
  });

  it("synthesizes a single retained option when facets is undefined", () => {
    const result = facetOptionsWithSelection(undefined, "대한민국");
    expect(result).toEqual([{ value: "대한민국", count: -1 }]);
  });

  it("returns an empty list when facets failed to load and nothing is selected", () => {
    expect(facetOptionsWithSelection(null, "")).toEqual([]);
  });

  it("does not treat a whitespace-only selection as active", () => {
    expect(facetOptionsWithSelection(null, "   ")).toEqual([]);
  });
});

describe("isRetainedFacetOption", () => {
  it("is true for a synthesized retained option", () => {
    expect(isRetainedFacetOption({ value: "미국", count: -1 })).toBe(true);
  });

  it("is false for a normal facet with a real count", () => {
    expect(isRetainedFacetOption({ value: "호주", count: 45 })).toBe(false);
  });

  it("is false for a facet with a zero count", () => {
    expect(isRetainedFacetOption({ value: "호주", count: 0 })).toBe(false);
  });
});
