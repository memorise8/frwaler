import { NextRequest, NextResponse } from 'next/server';
import { getPapers } from '@/lib/db';

export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const siteId = searchParams.get('site') || undefined;
  const search = searchParams.get('q') || undefined;
  const page = parseInt(searchParams.get('page') || '1', 10);
  const pageSize = parseInt(searchParams.get('pageSize') || '20', 10);

  try {
    const result = getPapers({ siteId, search, page, pageSize });
    return NextResponse.json(result);
  } catch (e) {
    return NextResponse.json({ papers: [], total: 0, sites: [] }, { status: 500 });
  }
}
