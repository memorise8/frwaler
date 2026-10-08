import { describe, expect, it, vi, afterEach } from "vitest";
import { canRun, type Capabilities } from "../src/lib/capabilities";
vi.mock("server-only", () => ({}));
import { POST } from "../src/app/api/jobs/route";

const setting = {label:"test", support:"limited", allowed:true, requires_trial:true, reason:"trial"};
const capability: Capabilities = {site_id:"fixture",order:"ID",count_semantics:"new_documents",source_hash:"fixture",missing_credentials:[],
  credential:{name:null,required:false,configured:false,authentication:"not_verified"},limitations:[],
  modes:{incremental:setting,full:{...setting,allowed:false},backfill:setting}};
afterEach(()=>vi.unstubAllGlobals());
describe("capability execution contract",()=>{
  it("requires loaded capabilities and explicit trial",()=>{
    expect(canRun(null,"incremental",true)).toBe(false);
    expect(canRun(capability,"incremental",false)).toBe(false);
    expect(canRun(capability,"incremental",true)).toBe(true);
  });
  it("blocks unsupported modes and missing credentials",()=>{
    expect(canRun(capability,"full",true)).toBe(false);
    expect(canRun({...capability,missing_credentials:["FIXTURE_KEY"]},"incremental",true)).toBe(false);
  });
  it("forwards backfill without silently changing to incremental",async()=>{
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({id:12}),{status:201}));vi.stubGlobal("fetch",fetch);
    const response=await POST(new Request("http://localhost/api/jobs",{method:"POST",body:JSON.stringify({siteId:"fixture",mode:"backfill",limit:3,trial:true})}));
    expect(response.status).toBe(201);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toMatchObject({mode:"backfill",limit_n:3,trial:true});
  });
  it("rejects unknown modes without calling backend",async()=>{
    const fetch=vi.fn();vi.stubGlobal("fetch",fetch);
    const response=await POST(new Request("http://localhost/api/jobs",{method:"POST",body:JSON.stringify({siteId:"fixture",mode:"typo",limit:3})}));
    expect(response.status).toBe(400);expect(fetch).not.toHaveBeenCalled();
  });
});
