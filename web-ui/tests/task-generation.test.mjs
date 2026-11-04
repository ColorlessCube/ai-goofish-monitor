import assert from 'node:assert/strict'
import test from 'node:test'
import { effectScope } from 'vue'
import { fakeWindow, sourceLoader } from './load-source.mjs'

function job(status, extra = {}) {
  return { job_id: 'job-1', task_name: 'Camera', status, steps: [], task: null, error: null, ...extra }
}

function polling(t, response) {
  const clock = fakeWindow(t)
  let reads = 0
  const load = sourceLoader({ '@/api/tasks': { getTaskGenerationJob: async () => { reads++; return response } } })
  const scope = effectScope()
  const state = scope.run(() => load('src/composables/useTaskGenerationJob.ts').useTaskGenerationJob())
  clock.onCleanup(() => scope.stop())
  return { ...clock, state, reads: () => reads }
}

for (const status of ['completed', 'failed', 'partial_success', 'unknown']) {
  test(`${status}: a terminal initial receipt never starts polling`, (t) => {
    const { state, timers } = polling(t, job(status))
    state.beginPolling(job(status))
    assert.equal(state.isGenerating.value, false)
    assert.equal(timers.size, 0)
    assert.equal(state.activeJob.value.status, status)
  })

  test(`${status}: a terminal polled receipt stops future requests`, async (t) => {
    const { state, timers, tick, reads } = polling(t, job(status))
    state.beginPolling(job('running'))
    assert.equal(timers.size, 1)
    await tick()
    assert.equal(state.activeJob.value.status, status)
    assert.equal(state.isGenerating.value, false)
    assert.equal(timers.size, 0)
    await tick()
    assert.equal(reads(), 1)
  })
}

test('sparse unknown receipt preserves earlier task snapshot and commitment evidence', async (t) => {
  const snapshot = { id: 7, instance_id: 'immutable-instance-7', task_name: 'Camera' }
  const { state } = polling(t, { job_id: 'job-1', status: 'unknown', retry_safe: false })
  state.beginPolling(job('running', { task: snapshot, task_committed: true }))
  await state.refreshJob()
  assert.equal(state.activeJob.value.status, 'unknown')
  assert.deepEqual(state.activeJob.value.task, snapshot)
  assert.equal(state.activeJob.value.task_committed, true)
  assert.equal(state.activeJob.value.task_name, 'Camera')
})

test('a late poll cannot resurrect a cleared job or restart its timer', async (t) => {
  let resolveResponse
  const response = new Promise((resolve) => { resolveResponse = resolve })
  const { state, timers } = polling(t, response)
  state.beginPolling(job('running'))
  const pending = state.refreshJob()
  state.clearJob()
  resolveResponse(job('running'))
  await pending
  assert.equal(state.activeJob.value, null)
  assert.equal(timers.size, 0)
})

for (const status of ['queued', 'running']) {
  test(`${status}: nonterminal jobs continue polling`, async (t) => {
    const { state, timers, tick, reads } = polling(t, job(status))
    state.beginPolling(job(status))
    await tick()
    assert.equal(state.isGenerating.value, true)
    assert.equal(timers.size, 1)
    assert.equal(reads(), 1)
    state.clearJob()
    assert.equal(timers.size, 0)
  })
}
