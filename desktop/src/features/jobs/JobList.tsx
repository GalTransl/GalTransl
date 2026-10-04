import { t as translate, useUiLanguage } from "../../i18n";
import { Button } from '../../components/Button';
import { EmptyState } from '../../components/EmptyState';
import { Panel } from '../../components/Panel';
import { InlineFeedback } from '../../components/page-state/InlineFeedback';
import type { Job } from '../../lib/api';
import { JobCard } from './JobCard';

type JobListProps = {
  jobs: Job[];
  jobsError: string | null;
  loading: boolean;
  onRefresh: () => void;
  refreshing: boolean;
};

export function JobList({ jobs, jobsError, loading, onRefresh, refreshing }: JobListProps) {
  useUiLanguage();
  return (
    <Panel
      title={translate("common:jobList.jobList_title_jobs")}
      description={translate("common:jobList.jobList_description_allJobRunningJobAutoUpdateStatus")}
      actions={
        <Button disabled={refreshing} onClick={onRefresh} variant="secondary">
          {refreshing ? translate("common:jobList.jobList_message_text") : translate("common:jobList.jobList_message_textVariant2")}
        </Button>
      }
    >
      {jobsError ? <InlineFeedback tone="error" title={translate("common:jobList.jobList_title_loadJobFailed")} description={jobsError} /> : null}

      {loading ? <EmptyState title={translate("common:jobList.jobList_title_pendingJob")} description={translate("common:jobList.jobList_description_pendingBackendJob")} /> : null}

      {!loading && jobs.length === 0 ? (
        <EmptyState
          title={translate("common:jobList.jobList_title_emptyJob")}
          description={translate("common:jobList.jobList_description_selectTranslationSubmitCountProjectJobStatus")}
        />
      ) : null}

      {!loading && jobs.length > 0 ? (
        <div className="job-list">
          {jobs.map((job) => (
            <JobCard job={job} key={job.job_id} />
          ))}
        </div>
      ) : null}
    </Panel>
  );
}
