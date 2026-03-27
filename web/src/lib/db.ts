import Database from 'better-sqlite3';
import path from 'path';

const DB_PATH = path.join(process.cwd(), '..', 'data', 'papers.db');

export interface Paper {
  id: string;
  site_id: string;
  external_id: string;
  title: string;
  authors: string | null;
  abstract: string | null;
  category: string | null;
  keywords: string | null;
  published_date: string | null;
  url: string;
  pdf_url: string | null;
  doi: string | null;
  department: string | null;
  metadata: string | null;
  crawled_at: string;
  summary: string | null;
}

export interface Site {
  id: string;
  name: string;
  base_url: string;
}

function getDb() {
  return new Database(DB_PATH, { readonly: true });
}

export function getPapers(options: {
  siteId?: string;
  search?: string;
  page?: number;
  pageSize?: number;
}): { papers: Paper[]; total: number; sites: Site[] } {
  const db = getDb();
  const { siteId, search, page = 1, pageSize = 20 } = options;

  let where: string[] = [];
  let params: any[] = [];

  if (siteId) {
    where.push('p.site_id = ?');
    params.push(siteId);
  }
  if (search) {
    where.push('(p.title LIKE ? OR p.keywords LIKE ?)');
    params.push(`%${search}%`, `%${search}%`);
  }

  const whereClause = where.length > 0 ? `WHERE ${where.join(' AND ')}` : '';

  const countRow = db.prepare(`SELECT COUNT(*) as count FROM papers p ${whereClause}`).get(...params) as any;
  const total = countRow?.count || 0;

  const offset = (page - 1) * pageSize;
  const papers = db.prepare(
    `SELECT p.* FROM papers p ${whereClause} ORDER BY p.published_date DESC, p.crawled_at DESC LIMIT ? OFFSET ?`
  ).all(...params, pageSize, offset) as Paper[];

  const sites = db.prepare('SELECT * FROM sites').all() as Site[];

  db.close();
  return { papers, total, sites };
}

export function getPaperById(id: string): (Paper & { summary?: string | null }) | null {
  const db = getDb();
  const paper = db.prepare('SELECT * FROM papers WHERE id = ?').get(id) as (Paper & { summary?: string | null }) | null;
  db.close();
  return paper;
}

export function getSiteOverview(): Array<Site & { paper_count: number; last_crawled: string | null }> {
  const db = getDb();
  const sites = db.prepare(`
    SELECT s.id, s.name, s.base_url,
           COUNT(p.id) as paper_count,
           MAX(p.crawled_at) as last_crawled
    FROM sites s
    LEFT JOIN papers p ON p.site_id = s.id
    GROUP BY s.id, s.name, s.base_url
    ORDER BY paper_count DESC
  `).all() as Array<Site & { paper_count: number; last_crawled: string | null }>;
  db.close();
  return sites;
}
