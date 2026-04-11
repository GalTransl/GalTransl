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
          <dt>Job ID</dt>
          <dd>{job.job_id}</dd>
        </div>
        <div>
          <dt>Result</dt>
          <dd>{formatJobResult(job)}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{formatTimestamp(job.created_at)}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{formatTimestamp(job.started_at)}</dd>
        </div>
        <div>
          <dt>Finished</dt>
          <dd>{formatTimestamp(job.finished_at)}</dd>
        </div>
      </dl>

      {progress ? (
        <div className="job-card__progress">
          <div className="job-card__progress-meta">
            <strong>任务进度</strong>
            <span>{progress.translated}/{progress.total} · {progress.percent}%</span>
          </div>
          <div className="progress-bar progress-bar--small">
            <div className="progress-bar__fill" style={{ width: `${progress.percent}%` }} />
          </div>
          {progress.currentFile ? (
            <div className="job-card__progress-file" title={progress.currentFile}>
              当前文件：{progress.currentFile}
            </div>
          ) : null}
        </div>
      ) : null}

      {job.error ? (
        <div className="job-card__error" role="alert">
          <strong>Execution error</strong>
          <pre>{job.error}</pre>
        </div>
      ) : null}
    </article>
  );
}
