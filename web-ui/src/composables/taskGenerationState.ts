import type { TaskGenerationJob, TaskGenerationReceipt, TaskGenerationStatus } from '@/types/task.d.ts'

export function isTerminalStatus(status: TaskGenerationStatus) {
  return status === 'completed' || status === 'failed' || status === 'partial_success' || status === 'unknown'
}

const variants = {
  queued: 'outline',
  running: 'secondary',
  completed: 'default',
  failed: 'destructive',
  partial_success: 'outline',
  unknown: 'outline',
  committed: 'outline',
} as const satisfies Record<TaskGenerationStatus | 'committed', string>

export function hasCommittedTask(job: TaskGenerationReceipt) {
  return job.task_committed === true || job.task != null || job.status === 'completed' || job.status === 'partial_success'
}

export function canRetryGeneration(job: TaskGenerationReceipt | null) {
  return job?.status === 'failed' && job.retry_safe === true && job.task_committed === false && !hasCommittedTask(job)
}

export function shouldRefreshTaskList(job: TaskGenerationReceipt) {
  return isTerminalStatus(job.status) && (hasCommittedTask(job) || job.status === 'unknown')
}

export function mergeGenerationReceipt(previous: TaskGenerationJob, next: TaskGenerationJob): TaskGenerationJob {
  if (previous.job_id !== next.job_id) return next
  return {
    ...previous,
    ...next,
    // A sparse recovery receipt cannot erase evidence of an already-created task.
    task: previous.task ?? next.task,
    task_committed: hasCommittedTask(previous) ? true : next.task_committed,
    // Retry permission must be explicit in the latest response, never inherited.
    retry_safe: next.retry_safe,
  }
}

export function generationStatusMeta(status: TaskGenerationStatus | 'committed') {
  return {
    labelKey: `generationOutcome.status.${status}`,
    helperKey: `generationOutcome.helper.${status}`,
    variant: variants[status],
  }
}

// Presentation only: keep the raw receipt intact, and never let a contradictory
// failure label outweigh evidence that creating the task already had an effect.
export function generationReceiptMeta(job: TaskGenerationReceipt) {
  if (isTerminalStatus(job.status) && job.status !== 'completed' && hasCommittedTask(job)) {
    return generationStatusMeta(job.error === 'scheduler_sync_required' ? 'partial_success' : 'committed')
  }
  return generationStatusMeta(job.status)
}
