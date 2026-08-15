import { describe, expect, it } from "vitest";
import { describeBlockedCrawlerWarning } from "../src/lib/blocked-crawler-warning";

describe("describeBlockedCrawlerWarning", () => {
  it("warns for a crawler known to be IP-blocked", () => {
    const warning = describeBlockedCrawlerWarning({
      status: "unhealthy",
      category: "IP차단",
      reason: "우리 IP 차단(403/WAF)",
    });
    expect(warning).not.toBeNull();
    expect(warning).toContain("IP차단");
    expect(warning).toContain("우리 IP 차단(403/WAF)");
  });

  it("warns for a crawler known to be unreachable (폐쇄/네트워크)", () => {
    const warning = describeBlockedCrawlerWarning({
      status: "unhealthy",
      category: "폐쇄/네트워크",
      reason: "사이트 폐쇄/네트워크 불통(DNS·연결 실패)",
    });
    expect(warning).not.toBeNull();
    expect(warning).toContain("폐쇄/네트워크");
  });

  it("does not warn for a healthy crawler regardless of category text", () => {
    expect(describeBlockedCrawlerWarning({ status: "healthy", category: "IP차단", reason: "" })).toBeNull();
  });

  it("does not warn for unhealthy categories that are not about the target site", () => {
    expect(describeBlockedCrawlerWarning({ status: "unhealthy", category: "수리(코드)", reason: "파서 오류" })).toBeNull();
    expect(describeBlockedCrawlerWarning({ status: "unhealthy", category: "점검필요", reason: "" })).toBeNull();
  });

  it("does not warn when there is no category at all", () => {
    expect(describeBlockedCrawlerWarning({ status: "unhealthy", category: "", reason: "" })).toBeNull();
  });

  it("produces a well-formed message when reason is empty", () => {
    const warning = describeBlockedCrawlerWarning({ status: "unhealthy", category: "IP차단", reason: "" });
    expect(warning).not.toContain("()");
    expect(warning).not.toBeNull();
  });
});
