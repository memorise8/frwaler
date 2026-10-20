import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { config, constantTimeEqual, proxy, REALM_HEADER } from "@/proxy";
import { GET as healthz } from "@/app/healthz/route";

afterEach(() => vi.unstubAllEnvs());

const basic = (user: string, pass: string) => `Basic ${Buffer.from(`${user}:${pass}`, "utf8").toString("base64")}`;
const request = (path: string, init: { method?: string; auth?: string } = {}) =>
  new NextRequest(`http://localhost${path}`, { method: init.method ?? "GET", headers: init.auth ? { authorization: init.auth } : {} });
const passed = (response: Response) => response.headers.get("x-middleware-next") === "1";
const withCreds = () => { vi.stubEnv("FE_BASIC_USER", "operator"); vi.stubEnv("FE_BASIC_PASS", "s3cret-비번"); };

describe("Basic auth proxy", () => {
  it.each([["both missing", "", ""], ["user missing", "", "p"], ["pass missing", "u", ""]])("fails closed when %s", (_label, user, pass) => {
    vi.stubEnv("FE_BASIC_USER", user);
    vi.stubEnv("FE_BASIC_PASS", pass);
    const response = proxy(request("/", { auth: basic(user, pass) }));
    expect(passed(response)).toBe(false);
    expect(response.status).toBe(503);
  });

  it("passes correct credentials, including UTF-8 passwords", () => {
    withCreds();
    expect(passed(proxy(request("/site-collect", { auth: basic("operator", "s3cret-비번") })))).toBe(true);
  });

  it.each([
    ["wrong password", basic("operator", "nope")],
    ["wrong user", basic("admin", "s3cret-비번")],
    ["password prefix", basic("operator", "s3cret")],
    ["no colon", `Basic ${Buffer.from("operator").toString("base64")}`],
    ["bearer scheme", "Bearer abc"],
    ["absent", undefined],
  ])("rejects %s with 401 and the realm header, no body", async (_label, auth) => {
    withCreds();
    const response = proxy(request("/", { auth }));
    expect(response.status).toBe(401);
    expect(response.headers.get("www-authenticate")).toBe(REALM_HEADER);
    expect(await response.text()).toBe("");
  });

  it("uses the exact realm", () => {
    expect(REALM_HEADER).toBe('Basic realm="Dashboard Admin Area", charset="UTF-8"');
  });

  it("blocks POST /api/collect/runs without credentials", () => {
    withCreds();
    const response = proxy(request("/api/collect/runs", { method: "POST" }));
    expect(response.status).toBe(401);
  });

  it("leaves GET /healthz and /_next/static open, but nothing else", () => {
    withCreds();
    expect(passed(proxy(request("/healthz")))).toBe(true);
    expect(passed(proxy(request("/_next/static/chunks/app.js")))).toBe(true);
    expect(passed(proxy(request("/favicon.ico")))).toBe(true);
    expect(proxy(request("/healthz", { method: "POST" })).status).toBe(401);
    expect(proxy(request("/healthz/x")).status).toBe(401);
    expect(proxy(request("/_next/image?url=x")).status).toBe(401);
    expect(proxy(request("/_next/staticx")).status).toBe(401);
  });

  it("keeps the matcher covering pages and /api but not static assets", () => {
    const pattern = new RegExp(`^${config.matcher[0]}$`);
    for (const path of ["/", "/site-collect", "/api/collect/runs", "/api/jobs/1/cancel", "/healthz", "/_next/data/x.json"]) expect(pattern.test(path), path).toBe(true);
    for (const path of ["/_next/static/chunks/a.js", "/favicon.ico"]) expect(pattern.test(path), path).toBe(false);
  });

  it("healthz returns a bare ok", async () => {
    const response = healthz();
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("ok");
  });
});

describe("constantTimeEqual", () => {
  it("compares by full content and length", () => {
    expect(constantTimeEqual("abc", "abc")).toBe(true);
    expect(constantTimeEqual("abc", "abd")).toBe(false);
    expect(constantTimeEqual("abc", "abcd")).toBe(false);
    expect(constantTimeEqual("", "a")).toBe(false);
    expect(constantTimeEqual("ab\u0000", "ab")).toBe(false);
    expect(constantTimeEqual("비번", "비번")).toBe(true);
  });
});
