import type { GeneratedTaskSnapshot, TaskCreateResponse, TaskGenerationJob } from '../src/types/task.d.ts'

declare const creationSnapshot: GeneratedTaskSnapshot

// Synchronous keyword responses have no job and may omit message entirely.
export const synchronousRepair = {
  status: 'partial_success', task_committed: true, retry_safe: false,
  error: 'scheduler_sync_required', task: creationSnapshot,
} satisfies TaskCreateResponse

export const synchronousUnknown = {
  status: 'unknown', task_committed: null, retry_safe: false, task: null,
} satisfies TaskCreateResponse

export const directKeywordSuccess = { task: creationSnapshot } satisfies TaskCreateResponse

// The live Pydantic model emits null before commitment is known.
export const liveJob = {
  job_id: 'queued-job', task_name: 'Camera', status: 'queued',
  message: 'Queued', current_step: null, steps: [], task: null, error: null,
  task_committed: null, retry_safe: false,
} satisfies TaskGenerationJob

// Missing-history/restart responses are deliberately sparse.
export const unknownJob = {
  job_id: 'missing-job', status: 'unknown', retry_safe: false,
} satisfies TaskGenerationJob

export const repairJob = {
  ...liveJob, status: 'partial_success', task_committed: true,
  error: 'scheduler_sync_required', retry_safe: false,
} satisfies TaskGenerationJob
