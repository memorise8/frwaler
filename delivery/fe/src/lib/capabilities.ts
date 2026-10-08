export type CrawlMode = "incremental" | "full" | "backfill";
export type Capabilities = {
  site_id: string;
  order: string;
  count_semantics: string;
  source_hash: string;
  modes: Record<CrawlMode, {label: string; support: string; allowed: boolean; requires_trial: boolean; reason: string}>;
  missing_credentials: string[];
  credential: {name: string | null; required: boolean; configured: boolean; authentication: string};
  limitations: string[];
};
export function canRun(capability: Capabilities | null, mode: CrawlMode, trial: boolean): boolean {
  const setting = capability?.modes[mode];
  return Boolean(setting?.allowed && (!setting.requires_trial || trial) && capability?.missing_credentials.length === 0);
}
