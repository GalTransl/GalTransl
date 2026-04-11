import { Button } from '../../components/Button';
import { EmptyState } from '../../components/EmptyState';
import { Panel } from '../../components/Panel';
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
  return (
    <Panel
      title="Jobs"
      description="展示全部本地任务，运行中的任务会通过轮询自动更新状态与错误信息。"
      actions={
        <Button disabled={refreshing} onClick={onRefresh} variant="secondary">
          {refreshing ? '刷新中…' : '刷新列表'}
        </Button>
      }
    >
      {jobsError ? (
        <div className="inline-alert inline-alert--error" role="alert">
          {jobsError}
        </div>
      ) : null}

      {loading ? <EmptyState title="正在载入任务" description="正在请求后端任务列表，请稍候。" /> : null}

      {!loading && jobs.length === 0 ? (
        <EmptyState
          title="还没有任务"
          description="先在左侧选择翻译模板并提交一个本地项目，任务状态会显示在这里。"
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
