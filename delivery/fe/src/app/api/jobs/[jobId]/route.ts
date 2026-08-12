import {NextResponse} from "next/server";
const backend=()=>(process.env.BE_URL??"http://127.0.0.1:8080").replace(/\/$/,"");
export async function GET(_request:Request,{params}:{params:Promise<{jobId:string}>}){const {jobId}=await params;if(!/^\d+$/.test(jobId))return NextResponse.json({detail:"invalid job"},{status:422});try{const r=await fetch(`${backend()}/jobs/${jobId}`,{cache:"no-store",signal:AbortSignal.timeout(8000)});return NextResponse.json(await r.json(),{status:r.status});}catch{return NextResponse.json({detail:"backend unavailable"},{status:503});}}
