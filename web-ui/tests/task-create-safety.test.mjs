import assert from 'node:assert/strict'
import test from 'node:test'
import { fakeWindow } from './load-source.mjs'
import { allNodes, findNode, flush, mountSource, text } from './vue-harness.mjs'

const request = { task_name: 'Camera', keyword: 'camera' }
const snapshot = { id: 7, instance_id: 'immutable-instance-7', task_name: 'Camera' }

for (const locale of ['zh-CN', 'en-US']) {
  for (const status of ['partial_success', 'unknown']) {
    test(`${locale}: synchronous ${status} retains a receipt without inventing or polling a job`, async (t) => {
      const clock = fakeWindow(t)
      let creates = 0
      let refreshes = 0
      let reads = 0
      const receipt = status === 'partial_success'
        ? { status, task_committed: true, retry_safe: false, error: 'scheduler_sync_required', task: snapshot }
        : { status, retry_safe: false }
      const { root, notices } = mountSource({ after: clock.onCleanup }, 'src/components/tasks/TaskCreateDialog.vue', { onCreated: () => refreshes++ }, {
        '@/api/tasks': {
          createTaskWithAI: async () => { creates++; return receipt },
          getTaskGenerationJob: async () => { reads++; throw new Error('A synchronous receipt has no job to poll') },
        },
      }, locale)
      await findNode(root, 'TaskForm').props.onSubmit({ ...request, decision_mode: 'keyword' })
      await flush()
      const output = text(root)
      assert.match(output, status === 'partial_success'
        ? (locale === 'zh-CN' ? /调度.*修复/ : /scheduler.*repair/i)
        : (locale === 'zh-CN' ? /结果.*未确认/ : /outcome.*unknown/i))
      assert.match(output, locale === 'zh-CN' ? /不要.*重新生成/ : /do not.*regenerate/i)
      assert.doesNotMatch(output, /作业 ID|Job ID/)
      if (status === 'partial_success') assert.match(output, /immutable-instance-7/)
      assert.equal(refreshes, 1)
      assert.equal(notices.some((notice) => /创建成功|创建任务失败|Task created successfully|Failed to create task/i.test(notice.title)), false)
      assert.equal(allNodes(root).find((entry) => entry.props.form === 'task-form').props.disabled, true)
      const progress = allNodes(root).filter((entry) => entry.type === 'Dialog')[1]
      assert.equal(progress.props.open, true)
      progress.props['onUpdate:open'](false)
      await flush()
      allNodes(root).find((entry) => entry.type === 'Button' && /查看生成回执|View generation receipt/.test(text(entry))).props.onClick()
      await flush()
      assert.equal(progress.props.open, true)
      assert.match(text(root), status === 'partial_success' ? /immutable-instance-7/ : /结果.*未确认|outcome.*unknown/i)
      await findNode(root, 'TaskForm').props.onSubmit(request)
      await clock.tick()
      assert.equal(creates, 1)
      assert.equal(reads, 0)
      assert.equal(clock.timers.size, 0)
      assert.equal(receipt.status, status, 'display must not rewrite the server receipt')
      assert.equal(receipt.job_id, undefined)
    })
  }
}

for (const locale of ['zh-CN', 'en-US']) {
  for (const error of ['scheduler_sync_required', 'receipt_conflict']) {
    test(`${locale}: committed failed toast matches the rendered badge for ${error}`, async (t) => {
      const clock = fakeWindow(t)
      let creates = 0
      let refreshes = 0
      const { root, notices } = mountSource({ after: clock.onCleanup }, 'src/components/tasks/TaskCreateDialog.vue', { onCreated: () => refreshes++ }, {
        '@/api/tasks': {
          createTaskWithAI: async () => { creates++; return { job: { job_id: 'job-1', status: 'queued' } } },
          getTaskGenerationJob: async () => ({ job_id: 'job-1', status: 'failed', task_committed: true, retry_safe: true, error, task: snapshot }),
        },
      }, locale)
      await findNode(root, 'TaskForm').props.onSubmit(request)
      await clock.tick()
      await flush()
      const badge = findNode(root, 'Badge')
      const notice = notices.at(-1)
      assert.equal(notice.title, text(badge).trim(), 'toast and progress must use the same evidence-aware outcome')
      assert.match(notice.title, locale === 'zh-CN' ? /任务已创建/ : /task created/i)
      assert.notEqual(notice.variant, 'destructive')
      assert.ok(text(root).includes(notice.description))
      assert.equal(refreshes, 1)
      await findNode(root, 'TaskForm').props.onSubmit(request)
      assert.equal(creates, 1)
      assert.equal(clock.timers.size, 0)
    })
  }
}

function createDialog(t, finalReceipt) {
  const clock = fakeWindow(t)
  let creates = 0
  let refreshes = 0
  const mounted = mountSource({ after: clock.onCleanup }, 'src/components/tasks/TaskCreateDialog.vue', { onCreated: () => refreshes++ }, {
    '@/api/tasks': {
      createTaskWithAI: async () => { creates++; return { job: { job_id: 'job-1', status: 'queued' } } },
      getTaskGenerationJob: async () => ({ job_id: 'job-1', ...finalReceipt }),
    },
  })
  return {
    ...clock, ...mounted,
    creates: () => creates,
    refreshes: () => refreshes,
    submit: () => findNode(mounted.root, 'TaskForm').props.onSubmit(request),
  }
}

for (const status of ['partial_success', 'unknown']) {
  test(`${status}: refresh list, retain receipt, and prevent a second generation request`, async (t) => {
    const ui = createDialog(t, { status, task_committed: true, retry_safe: false, task: snapshot })
    await ui.submit()
    await ui.tick()
    await flush()
    assert.equal(ui.refreshes(), 1, 'list must refresh without reporting full success')
    assert.equal(ui.timers.size, 0)
    assert.match(text(ui.root), /immutable-instance-7/)
    assert.equal(allNodes(ui.root).filter((entry) => entry.type === 'Dialog')[1].props.open, true)
    assert.equal(ui.notices.some((notice) => /创建成功|创建任务失败/.test(notice.title)), false)
    assert.equal(allNodes(ui.root).find((entry) => entry.props.form === 'task-form').props.disabled, true)
    const progress = allNodes(ui.root).filter((entry) => entry.type === 'Dialog')[1]
    progress.props['onUpdate:open'](false)
    await flush()
    allNodes(ui.root).find((entry) => entry.type === 'Button' && /查看生成回执/.test(text(entry))).props.onClick()
    await flush()
    assert.equal(progress.props.open, true)
    assert.match(text(ui.root), /immutable-instance-7/)
    await ui.submit() // bypass disabled button to exercise the handler's safety gate
    assert.equal(ui.creates(), 1)
  })
}

test('unknown without a snapshot still refreshes the list without asserting creation failed', async (t) => {
  const ui = createDialog(t, { status: 'unknown', retry_safe: false })
  await ui.submit()
  await ui.tick()
  await flush()
  assert.equal(ui.refreshes(), 1)
  await ui.submit()
  assert.equal(ui.creates(), 1)
})

for (const [label, metadata, mayRetry] of [
  ['explicitly safe', { task_committed: false, retry_safe: true }, true],
  ['legacy metadata absent', {}, false],
  ['retry unsafe', { task_committed: false, retry_safe: false }, false],
  ['commit uncertain', { retry_safe: true }, false],
  ['committed', { task_committed: true, retry_safe: true }, false],
  ['snapshot overrides contradictory flags', { task_committed: false, retry_safe: true, task: snapshot }, false],
]) {
  test(`failed ${label}: retry permission is explicit and side-effect aware`, async (t) => {
    const ui = createDialog(t, { status: 'failed', ...metadata })
    await ui.submit()
    await ui.tick()
    await flush()
    const retry = allNodes(ui.root).find((entry) => entry.type === 'Button' && /重试生成/.test(text(entry)))
    assert.equal(Boolean(retry), mayRetry)
    if (retry) {
      retry.props.onClick()
      await flush()
      const dialogs = allNodes(ui.root).filter((entry) => entry.type === 'Dialog')
      assert.equal(dialogs[0].props.open, true)
      assert.equal(dialogs[1].props.open, false)
      assert.equal(ui.creates(), 1, 'retry must require an explicit form submission')
    }
    await ui.submit()
    assert.equal(ui.creates(), mayRetry ? 2 : 1)
  })
}

test('queued work cannot be submitted twice while its progress dialog is closed', async (t) => {
  const ui = createDialog(t, { status: 'running' })
  await ui.submit()
  await flush()
  allNodes(ui.root).filter((entry) => entry.type === 'Dialog')[1].props['onUpdate:open'](false)
  await ui.submit()
  assert.equal(ui.creates(), 1)
})

test('lost POST response is uncertain, refreshes the list and blocks blind resubmission', async (t) => {
  const clock = fakeWindow(t)
  let creates = 0
  let refreshes = 0
  const { root, notices } = mountSource({ after: clock.onCleanup }, 'src/components/tasks/TaskCreateDialog.vue', { onCreated: () => refreshes++ }, {
    '@/api/tasks': { createTaskWithAI: async () => { creates++; throw new Error('connection lost after send') } },
  })
  await findNode(root, 'TaskForm').props.onSubmit(request)
  await flush()
  assert.equal(refreshes, 1)
  assert.match(text(root), /结果.*未确认/)
  assert.equal(notices.some((notice) => /创建任务失败/.test(notice.title)), false)
  await findNode(root, 'TaskForm').props.onSubmit(request)
  assert.equal(creates, 1)
})

test('a response without a job or task is not presented as successful creation', async (t) => {
  const clock = fakeWindow(t)
  let creates = 0
  const { root, notices } = mountSource({ after: clock.onCleanup }, 'src/components/tasks/TaskCreateDialog.vue', {}, {
    '@/api/tasks': { createTaskWithAI: async () => { creates++; return { message: 'Incomplete response' } } },
  })
  await findNode(root, 'TaskForm').props.onSubmit(request)
  await flush()
  assert.equal(notices.some((notice) => /创建成功/.test(notice.title)), false)
  assert.match(text(root), /结果.*未确认/)
  await findNode(root, 'TaskForm').props.onSubmit(request)
  assert.equal(creates, 1)
})

test('immediate keyword task creation preserves the existing success path', async (t) => {
  const clock = fakeWindow(t)
  let refreshes = 0
  const { root, notices } = mountSource({ after: clock.onCleanup }, 'src/components/tasks/TaskCreateDialog.vue', { onCreated: () => refreshes++ }, {
    '@/api/tasks': { createTaskWithAI: async () => ({ task: snapshot }) },
  })
  await findNode(root, 'TaskForm').props.onSubmit({ ...request, decision_mode: 'keyword' })
  await flush()
  assert.equal(refreshes, 1)
  assert.equal(notices.some((notice) => /创建成功/.test(notice.title)), true)
  assert.equal(clock.timers.size, 0)
})

test('normal completion refreshes the list and preserves the normal success path', async (t) => {
  const ui = createDialog(t, { status: 'completed', task: snapshot })
  await ui.submit()
  await ui.tick()
  await flush()
  assert.equal(ui.refreshes(), 1)
  assert.equal(ui.notices.some((notice) => /创建成功/.test(notice.title)), true)
  assert.equal(ui.timers.size, 0)
  await ui.submit()
  assert.equal(ui.creates(), 2)
})
