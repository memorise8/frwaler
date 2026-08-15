import { describe, expect, it } from "vitest";
import { findUncataloguedSites } from "../src/lib/uncatalogued-sites";

describe("findUncataloguedSites", () => {
  it("returns nothing when every database site has a catalogue entry", () => {
    const bySite = [{ key: "site-a", documents: 100 }, { key: "site-b", documents: 50 }];
    expect(findUncataloguedSites(bySite, ["site-a", "site-b"])).toEqual([]);
  });

  it("reports a single site present in the database but missing from the catalogue", () => {
    const bySite = [{ key: "site-a", documents: 100 }, { key: "scienceon-api", documents: 10447 }];
    expect(findUncataloguedSites(bySite, ["site-a"])).toEqual([{ siteId: "scienceon-api", documents: 10447 }]);
  });

  it("reports several uncatalogued sites", () => {
    const bySite = [
      { key: "site-a", documents: 100 },
      { key: "scienceon-api", documents: 10447 },
      { key: "another-api", documents: 5 },
    ];
    expect(findUncataloguedSites(bySite, ["site-a"])).toEqual([
      { siteId: "scienceon-api", documents: 10447 },
      { siteId: "another-api", documents: 5 },
    ]);
  });

  it("includes an uncatalogued site that has zero documents", () => {
    const bySite = [{ key: "site-a", documents: 100 }, { key: "empty-api", documents: 0 }];
    expect(findUncataloguedSites(bySite, ["site-a"])).toEqual([{ siteId: "empty-api", documents: 0 }]);
  });

  it("does not report a catalogue entry that has no matching database row", () => {
    const bySite = [{ key: "site-a", documents: 100 }];
    expect(findUncataloguedSites(bySite, ["site-a", "site-with-no-documents-yet"])).toEqual([]);
  });
});
