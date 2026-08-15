export type JobFailureSample = Readonly<{ status: string; error_code?: string | null }>;

export type ConfigurationFailureSummary = Readonly<{
  hasConfigurationFailures: boolean;
  failingCount: number;
}>;

// provider_from_env (delivery/translation/providers.py:268-287) raises
// ProviderError("configuration", ...) whenever the selected provider's
// endpoint/model env vars are missing, or the queued model no longer
// matches the server's configured one. The job's ProviderError handler
// (delivery/translation/jobs.py:256-268) persists that as a job with
// status='failed' and error_code='configuration' -- and since
// ProviderError("configuration", ...) is always raised with
// retryable=False, it never becomes 'pending' again on its own. That is
// the one error_code this function keys off of: it means every job queued
// against this provider/model/prompt combination will fail the same way
// until the server's environment is fixed, which is worth surfacing
// beyond the per-row status the operator would otherwise have to notice.
export const CONFIGURATION_ERROR_CODE = "configuration";

export const detectConfigurationFailures = (jobs: readonly JobFailureSample[]): ConfigurationFailureSummary => {
  const failingCount = jobs.filter((job) => job.status === "failed" && job.error_code === CONFIGURATION_ERROR_CODE).length;
  return { hasConfigurationFailures: failingCount > 0, failingCount };
};
