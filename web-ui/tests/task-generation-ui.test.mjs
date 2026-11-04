import assert from 'node:assert/strict'
import test from 'node:test'
import { allNodes, findNode, mountSource, text } from './vue-harness.mjs'

const snapshot = { id: 7, instance_id: 'immutable-instance-7', task_name: 'Camera' }

for (const locale of ['zh-CN', 'en-US']) {
  for (const [label, fields] of [
    ['scheduler repair evidence', { status: 'failed', task_committed: true, task: snapshot, error: 'scheduler_sync_required' }],
    ['other failure after commit', { status: 'failed', task_committed: true, task: snapshot, error: 'receipt_conflict' }],
    ['snapshot overrides false commit flag', { status: 'failed', task_committed: false, task: snapshot }],
    ['commit flag without snapshot', { status: 'failed', task_committed: true }],
    ['partial success without scheduler evidence', { status: 'partial_success', task: snapshot }],
    ['unknown with preserved commit evidence', { status: 'unknown', task: snapshot }],
  ]) {
    test(`${locale}: ${label} renders the committed outcome without claiming creation failed`, (t) => {
      const job = { job_id: 'job-1', retry_safe: true, ...fields }
      const original = structuredClone(job)
      const { root } = mountSource(t, 'src/components/tasks/TaskGenerationDialog.vue', { open: true, job }, {}, locale)
      const output = text(root)
      assert.match(output, locale === 'zh-CN' ? /任务已创建/ : /task created/i)
      assert.doesNotMatch(output, /生成失败|Generation failed|创建失败|Creation failed/i)
      assert.match(output, locale === 'zh-CN' ? /不要.*重新生成/ : /do not.*regenerate/i)
      if (job.error === 'scheduler_sync_required') {
        assert.match(output, locale === 'zh-CN' ? /调度.*修复/ : /scheduler.*repair/i)
      } else {
        assert.match(output, locale === 'zh-CN' ? /后续状态.*核对/ : /follow-up.*verif/i)
        assert.doesNotMatch(output, /调度|scheduler|同步尚未完成|synchronization needs repair/i)
      }
      assert.notEqual(findNode(root, 'Badge').props.variant, 'destructive')
      assert.equal(allNodes(root).some((entry) => entry.type === 'Button' && /重试生成|Retry generation/.test(text(entry))), false)
      if (job.task) assert.match(output, /immutable-instance-7/)
      if (job.error) {
        const error = allNodes(root).find((entry) => entry.type === 'p' && entry.text === job.error)
        assert.ok(error, 'raw error remains available')
        assert.doesNotMatch(error.props.class, /red-/)
      }
      assert.deepEqual(job, original, 'effective presentation must preserve the raw receipt')
    })
  }

  test(`${locale}: partial success says committed with scheduler repair, not queued or fully successful`, (t) => {
    const { root } = mountSource(t, 'src/components/tasks/TaskGenerationProgress.vue', {
      job: { job_id: 'job-1', status: 'partial_success', task_committed: true, retry_safe: false, task: snapshot, error: 'scheduler_sync_required' },
    }, {}, locale)
    const output = text(root)
    assert.match(output, locale === 'zh-CN' ? /调度.*修复/ : /scheduler.*repair/i)
    assert.match(output, /immutable-instance-7/)
    assert.match(output, /job-1/)
    assert.doesNotMatch(output, /排队中|Queued|已完成|Completed/)
  })

  test(`${locale}: sparse unknown receipt explains uncertainty and no regeneration`, (t) => {
    const { root } = mountSource(t, 'src/components/tasks/TaskGenerationDialog.vue', {
      open: true, job: { job_id: 'lost-job', status: 'unknown', retry_safe: false },
    }, {}, locale)
    const output = text(root)
    assert.match(output, locale === 'zh-CN' ? /结果.*未确认/ : /outcome.*unknown/i)
    assert.match(output, locale === 'zh-CN' ? /不要.*重新生成/ : /do not.*regenerate/i)
    assert.doesNotMatch(output, /排队中|Queued|生成失败|Failed|generating in the background|转入后台生成/)
  })
}
