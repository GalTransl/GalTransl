import { t as translate, useUiLanguage } from "../../i18n";
import { StatusBadge } from '../../components/StatusBadge';
import type { Job } from '../../lib/api';
import { formatJobResult, formatTimestamp } from '../../lib/format';

type JobCardProgress = {
  currentFile?: string;
  percent: number;
  total: number;
  translated: number;
};

type JobCardProps = {
  job: Job;
  progress?: JobCardProgress;
};

export function JobCard({ job, progress }: JobCardProps) {
  useUiLanguage();
  return (
    <article className="job-card">
      <div className="job-card__header">
        <div className="job-card__title-block">
          <h3 title={job.project_dir}>{job.project_dir}</h3>
          <p>
            {job.translator} · {job.config_file_name}
          </p>
        </div>

        <StatusBadge label={job.status} tone={job.status} />
      </div>

      <dl className="meta-grid">
        <div>
          <dt>{translate("common:jobCard.metaGrid_message_jobID")}</dt>
          <dd>{job.job_id}</dd>
        </div>
        <div>
          <dt>{translate("common:jobCard.metaGrid_message_result")}</dt>
          <dd>{formatJobResult(job)}</dd>
        </div>
        <div>
          <dt>{translate("common:jobCard.metaGrid_message_created")}</dt>
          <dd>{formatTimestamp(job.created_at)}</dd>
        </div>
        <div>
          <dt>{translate("common:jobCard.metaGrid_message_started")}</dt>
          <dd>{formatTimestamp(job.started_at)}</dd>
        </div>
        <div>
          <dt>{translate("common:jobCard.metaGrid_message_finished")}</dt>
          <dd>{formatTimestamp(job.finished_at)}</dd>
        </div>
      </dl>

      {progress ? (
        <div className="job-card__progress">
          <div className="job-card__progress-meta">
            <strong>{translate("common:jobCard.jobCardProgressMeta_message_jobProgress")}</strong>
            <span>{progress.translated}/{progress.total} · {progress.percent}%</span>
          </div>
          <div className="progress-bar progress-bar--small">
            <div className="progress-bar__fill" style={{ width: `${progress.percent}%` }} />
          </div>
          {progress.currentFile ? (
            <div className="job-card__progress-file" title={progress.currentFile}>{translate("common:jobCard.jobCardProgress_message_currentFile", { currentFile: progress.currentFile })}</div>
          ) : null}
        </div>
      ) : null}

      {job.error ? (
        <div className="job-card__error" role="alert">
          <strong>{translate("common:jobCard.jobCardError_message_executionError")}</strong>
          <pre>{job.error}</pre>
        </div>
      ) : null}
    </article>
  );
}
