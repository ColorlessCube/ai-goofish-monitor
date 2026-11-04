import assert from 'node:assert/strict'
import test from 'node:test'
import { effectScope } from 'vue'
import { fakeWindow, sourceLoader } from './load-source.mjs'
import { allNodes, findNode, flush, mountSource, text } from './vue-harness.mjs'

const snapshot = { id: 7, instance_id: 'instance-7', task_name: 'Camera' }
const request = { task_name: 'Camera', keyword: 'camera', decision_mode: 'keyword' }

test('keyword partial_success response must not claim full success or allow regeneration', async (t) => {
  const clock = fakeWindow(t)
  let creates = 0
  let refreshes = 0
  const { root, notices } = mountSource({ after: clock.onCleanup }, 'src/components/tasks/TaskCreateDialog.vue', { onCreated: () => refreshes++ }, {
    '@/api/tasks': { createTaskWithAI: async () => {
      creates++
      return { status: 'partial_success', task_committed: true, retry_safe: false, error: 'scheduler_sync_required', task: snapshot }
    } },
  })
  await findNode(root, 'TaskForm').props.onSubmit(request)
  await flush()
  const firstNotices = [...notices]
  const output = text(root)
  const disabled = allNodes(root).find((entry) => entry.props.form === 'task-form').props.disabled
  await findNode(root, 'TaskForm').props.onSubmit(request)
  await flush()
  assert.equal(firstNotices.some((notice) => /创建成功/.test(notice.title)), false)
  assert.equal(creates, 1)
  assert.equal(refreshes, 1)
  assert.equal(disabled, true)
  assert.match(output, /调度.*修复/)
})

test('failed with committed task must not render Generation failed badge', (t) => {
  const clock = fakeWindow(t)
  const { root } = mountSource({ after: clock.onCleanup }, 'src/components/tasks/TaskGenerationDialog.vue', {
    open: true,
    job: { job_id: 'job-1', status: 'failed', task_committed: true, retry_safe: true, task: snapshot, error: 'scheduler_sync_required' },
  })
  const output = text(root)
  assert.doesNotMatch(output, /生成失败/)
})

test('old-job late completion and rejection do not overwrite a new active job', async (t) => {
  const clock = fakeWindow(t)
  let resolveOld
  let rejectNext
  const pendingOld = new Promise((resolve) => { resolveOld = resolve })
  const pendingNext = new Promise((_resolve, reject) => { rejectNext = reject })
  let calls = 0
  const load = sourceLoader({ '@/api/tasks': { getTaskGenerationJob: () => ++calls === 1 ? pendingOld : pendingNext } })
  const scope = effectScope()
  const state = scope.run(() => load('src/composables/useTaskGenerationJob.ts').useTaskGenerationJob())
  clock.onCleanup(() => scope.stop())
  state.beginPolling({ job_id: 'old-job', status: 'running' })
  const pending = state.refreshJob()
  state.beginPolling({ job_id: 'new-job', status: 'running' })
  resolveOld({ job_id: 'old-job', status: 'completed', task: snapshot })
  await pending
  assert.equal(state.activeJob.value.job_id, 'new-job')
  assert.equal(state.activeJob.value.task, undefined)
  assert.equal(clock.timers.size, 1)
  const request = state.refreshJob()
  scope.stop()
  rejectNext(new Error('late rejection after unmount'))
  await request
  assert.equal(state.activeJob.value, null)
  assert.equal(state.pollingError.value, null)
  assert.equal(clock.timers.size, 0)
})
