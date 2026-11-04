// Local messages shared by generation components; legacy global keys stay intact.
export const taskGenerationMessages = {
  'zh-CN': {
    generationOutcome: {
      receiptDescription: '查看本次生成请求的回执；任务快照不代表当前运行状态。',
      status: {
        queued: '排队中', running: '生成中', completed: '已完成', failed: '生成失败',
        partial_success: '任务已创建，调度待修复', unknown: '结果未确认',
        committed: '任务已创建，后续状态待核对',
      },
      helper: {
        queued: '请求已排队，请等待处理。',
        running: '正在生成分析标准并创建任务。',
        completed: '任务创建完成。',
        failed: '生成已结束。仅在服务明确确认未创建且可安全重试时，才能重新生成。',
        partial_success: '任务已保存，但调度同步尚未完成。请修复调度器并核对任务列表，不要重新生成，以免重复创建任务。',
        committed: '回执包含任务已创建的证据，后续状态仍需核对。请核对任务列表及回执，不要重新生成，以免重复创建任务。',
        unknown: '结果尚未确认，可能已创建任务。这不代表创建失败或未执行；请核对任务列表及作业回执，不要自动重试或重新生成。',
      },
      jobId: '作业 ID', snapshot: '创建时任务快照', taskId: '任务 ID', instanceId: '实例 ID',
      retry: '重试生成', viewReceipt: '查看生成回执',
    },
  },
  'en-US': {
    generationOutcome: {
      receiptDescription: 'Review this generation receipt. The task snapshot is not live runtime state.',
      status: {
        queued: 'Queued', running: 'Running', completed: 'Completed', failed: 'Generation failed',
        partial_success: 'Task created; scheduler repair required', unknown: 'Outcome unknown',
        committed: 'Task created; follow-up status needs verification',
      },
      helper: {
        queued: 'The request is queued for processing.',
        running: 'Generating analysis criteria and creating the task.',
        completed: 'Task creation completed.',
        failed: 'Generation ended. Regenerate only when the service explicitly confirms no task was committed and retry is safe.',
        partial_success: 'The task was saved, but scheduler synchronization needs repair. Repair the scheduler and check the task list. Do not regenerate: this could create a duplicate task.',
        committed: 'The receipt contains evidence that the task was created. Its follow-up status still needs verification. Check the task list and receipt. Do not regenerate: this could create a duplicate task.',
        unknown: 'The outcome is unknown and a task may already exist. This is not proof of failure or non-execution. Check the task list and receipt; do not automatically retry or regenerate.',
      },
      jobId: 'Job ID', snapshot: 'Creation-time task snapshot', taskId: 'Task ID', instanceId: 'Instance ID',
      retry: 'Retry generation', viewReceipt: 'View generation receipt',
    },
  },
}
