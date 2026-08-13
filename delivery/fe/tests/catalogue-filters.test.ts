import { describe, expect, it } from "vitest";
import { hasActiveCatalogueFilter } from "../src/lib/catalogue-filters";

const NONE = { query: "", status: "", category: "", country: "", docType: "" };

describe("hasActiveCatalogueFilter", () => {
  it("is false when no filter is set", () => {
    expect(hasActiveCatalogueFilter(NONE)).toBe(false);
  });

  it("is true when only the search query is set", () => {
    expect(hasActiveCatalogueFilter({ ...NONE, query: "seoul" })).toBe(true);
  });

  it("is true when only the status filter is set", () => {
    expect(hasActiveCatalogueFilter({ ...NONE, status: "healthy" })).toBe(true);
  });

  it("is true when only the failure category filter is set", () => {
    expect(hasActiveCatalogueFilter({ ...NONE, category: "timeout" })).toBe(true);
  });

  it("is true when only the country filter is set", () => {
    expect(hasActiveCatalogueFilter({ ...NONE, country: "대한민국" })).toBe(true);
  });

  it("is true when only the docType filter is set", () => {
    expect(hasActiveCatalogueFilter({ ...NONE, docType: "PDF" })).toBe(true);
  });

  it("is true when multiple filters are combined", () => {
    expect(hasActiveCatalogueFilter({ ...NONE, country: "대한민국", docType: "PDF" })).toBe(true);
    expect(hasActiveCatalogueFilter({ ...NONE, query: "seoul", status: "healthy", category: "timeout" })).toBe(true);
  });

  it("does not treat an empty string as an active filter", () => {
    expect(hasActiveCatalogueFilter({ query: "", status: "", category: "", country: "", docType: "" })).toBe(false);
  });

  it("does not treat a whitespace-only value as an active filter", () => {
    expect(hasActiveCatalogueFilter({ ...NONE, query: "   " })).toBe(false);
    expect(hasActiveCatalogueFilter({ ...NONE, status: " " })).toBe(false);
    expect(hasActiveCatalogueFilter({ ...NONE, category: "\t" })).toBe(false);
    expect(hasActiveCatalogueFilter({ ...NONE, country: "  " })).toBe(false);
    expect(hasActiveCatalogueFilter({ ...NONE, docType: " " })).toBe(false);
  });
});
