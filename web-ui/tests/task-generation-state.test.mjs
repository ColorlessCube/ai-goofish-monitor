import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import ts from 'typescript'
import { sourceLoader } from './load-source.mjs'

const state = sourceLoader()('src/composables/taskGenerationState.ts')

test('TypeScript accepts the actual live and sparse receipt JSON contracts', () => {
  const program = ts.createProgram([fileURLToPath(new URL('./receipt-contracts.ts', import.meta.url))], {
    noEmit: true, strict: true, skipLibCheck: true,
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.NodeNext,
    moduleResolution: ts.ModuleResolutionKind.NodeNext,
  })
  const diagnostics = ts.getPreEmitDiagnostics(program)
  assert.equal(diagnostics.length, 0, diagnostics.map((entry) => ts.flattenDiagnosticMessageText(entry.messageText, '\n')).join('\n'))
})

test('retry is never permitted for unknown, committed, or nonterminal states even with retry_safe=true', () => {
  for (const status of ['queued', 'running', 'completed', 'partial_success', 'unknown']) {
    assert.equal(state.canRetryGeneration({ job_id: 'job-1', status, task_committed: false, retry_safe: true }), false)
  }
  assert.equal(state.canRetryGeneration({ job_id: 'job-1', status: 'failed', task_committed: null, retry_safe: true }), false)
})

test('receipt merge keeps immutable identity, does not borrow identity across jobs, or inherit retry permission', () => {
  const previous = { job_id: 'job-1', status: 'running', task: { id: 7, instance_id: 'original' }, task_committed: true }
  const sparse = { job_id: 'job-1', status: 'unknown', task: null, task_committed: null, retry_safe: false }
  const merged = state.mergeGenerationReceipt(previous, sparse)
  assert.equal(merged.task, previous.task)
  assert.equal(merged.task_committed, true)
  assert.equal(merged.retry_safe, false)
  const other = { job_id: 'job-2', status: 'failed', task_committed: false, retry_safe: true }
  assert.equal(state.mergeGenerationReceipt(previous, other), other)
  const latest = state.mergeGenerationReceipt(other, { job_id: 'job-2', status: 'failed', task_committed: false })
  assert.equal(state.canRetryGeneration(latest), false)
})
