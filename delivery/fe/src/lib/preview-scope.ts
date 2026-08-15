// The backend's preview estimator (delivery/translation/jobs.py:82-106,
// preview_targets) reports two numbers of fundamentally different scope:
// `total` is every document matching the filters (the whole eligible
// population), while `estimated_prompt_tokens`/`estimated_seconds` are
// computed over `bounded = min(total, limit)` -- the number of jobs THIS
// request would actually enqueue (delivery/translation/safety.py:74-84,
// estimate). The backend never echoes `bounded` back, so the UI has to
// reconstruct it from the `limit` it sent, to make explicit that the time
// estimate covers the request, not the population.
export const requestedPreviewScope = (total: number, requestedLimit: number): number =>
  Math.max(0, Math.min(total, requestedLimit));
