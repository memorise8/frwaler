import { describe, expect, it } from "vitest";
import { groupDocuments } from "../src/lib/collected-breakdown";

const crawlers = new Map([
  ["kr", { siteName: "한국 사이트", country: "한국", docType: "보고서" }],
  ["plain", { siteName: "분류 없음", country: "기타", docType: "기타" }],
]);
const bySite = [
  { key: "kr", documents: 10 },
  { key: "plain", documents: 5 },
  { key: "api-a", documents: 30 },
  { key: "api-b", documents: 1 },
];

describe("groupDocuments", () => {
  it("totals by field with 기타 largest first", () => {
    expect(groupDocuments(bySite, crawlers, "country").rows).toEqual([["기타", 36], ["한국", 10]]);
  });

  it("splits 기타 into uncatalogued and unclassified parts with their largest sites", () => {
    const { otherParts } = groupDocuments(bySite, crawlers, "docType", 1);
    expect(otherParts.map((part) => [part.reason, part.documents, part.sites])).toEqual([
      ["uncatalogued", 31, 2], ["unclassified", 5, 1],
    ]);
    expect(otherParts[0]!.label).toBe("수집기 목록에 없는 사이트");
    expect(otherParts[0]!.topSites).toEqual([{ siteId: "api-a", name: "api-a", documents: 30 }]);
    expect(otherParts[1]!.label).toBe("수집기는 있으나 자료 유형이 정해지지 않은 사이트");
    expect(otherParts[1]!.topSites[0]!.name).toBe("분류 없음");
  });

  it("has no parts when nothing is 기타", () => {
    expect(groupDocuments([{ key: "kr", documents: 1 }], crawlers, "country").otherParts).toEqual([]);
  });
});
