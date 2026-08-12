import { NextResponse } from "next/server";

const backend=()=>(process.env.BE_URL??"http://127.0.0.1:8080").replace(/\/$/,"");
export async function GET(_request:Request,{params}:{params:Promise<{seqId:string;kind:string}>}){
  const {seqId,kind}=await params;
  if(!/^\d+$/.test(seqId)||!new Set(["pdf","text"]).has(kind))return NextResponse.json({detail:"invalid document file"},{status:422});
  try{const response=await fetch(`${backend()}/documents/${seqId}/${kind}`,{cache:"no-store",signal:AbortSignal.timeout(30000)});
    if(!response.ok)return NextResponse.json(await response.json().catch(()=>({detail:"file unavailable"})),{status:response.status});
    const headers=new Headers();for(const name of ["content-type","content-length","content-disposition","accept-ranges","content-range"]){const value=response.headers.get(name);if(value)headers.set(name,value);}
    return new NextResponse(response.body,{status:response.status,headers});
  }catch{return NextResponse.json({detail:"backend unavailable"},{status:503});}
}
