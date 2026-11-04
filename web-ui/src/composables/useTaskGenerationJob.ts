import { computed, onScopeDispose, ref } from 'vue'
import { getTaskGenerationJob } from '@/api/tasks'
import type { TaskGenerationJob } from '@/types/task.d.ts'

import { isTerminalStatus, mergeGenerationReceipt } from './taskGenerationState'

const POLL_INTERVAL_MS = 800

export function useTaskGenerationJob() {
  const activeJob = ref<TaskGenerationJob | null>(null)
  const pollingError = ref<Error | null>(null)
  const isPolling = ref(false)
  let pollTimer: ReturnType<typeof window.setTimeout> | null = null
  let pollVersion = 0

  function clearTimer() {
    if (pollTimer === null) return
    window.clearTimeout(pollTimer)
    pollTimer = null
  }

  async function refreshJob() {
    if (!activeJob.value) return
    const previousJob = activeJob.value
    const version = ++pollVersion
    clearTimer()
    try {
      const nextJob = await getTaskGenerationJob(previousJob.job_id)
      if (version !== pollVersion) return
      activeJob.value = mergeGenerationReceipt(previousJob, nextJob)
      pollingError.value = null
      if (isTerminalStatus(nextJob.status)) {
        isPolling.value = false
        clearTimer()
        return
      }
      isPolling.value = true
      scheduleNextPoll()
    } catch (error) {
      if (version !== pollVersion) return
      isPolling.value = false
      clearTimer()
      pollingError.value = error instanceof Error ? error : new Error(String(error))
    }
  }

  function scheduleNextPoll() {
    clearTimer()
    pollTimer = window.setTimeout(() => {
      void refreshJob()
    }, POLL_INTERVAL_MS)
  }

  function beginPolling(job: TaskGenerationJob) {
    pollVersion++
    activeJob.value = job
    pollingError.value = null
    if (isTerminalStatus(job.status)) {
      isPolling.value = false
      clearTimer()
      return
    }
    isPolling.value = true
    scheduleNextPoll()
  }

  function clearJob() {
    pollVersion++
    activeJob.value = null
    pollingError.value = null
    isPolling.value = false
    clearTimer()
  }

  onScopeDispose(clearJob)

  return {
    activeJob,
    pollingError,
    isGenerating: computed(() => isPolling.value),
    beginPolling,
    clearJob,
    refreshJob,
  }
}
