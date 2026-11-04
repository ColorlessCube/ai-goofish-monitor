<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Badge } from '@/components/ui/badge'
import type { TaskGenerationReceipt, TaskGenerationStep } from '@/types/task.d.ts'
import { generationReceiptMeta, isTerminalStatus } from '@/composables/taskGenerationState'
import { taskGenerationMessages } from '@/composables/taskGenerationMessages'

const props = defineProps<{
  job: TaskGenerationReceipt
}>()
const { t } = useI18n({ useScope: 'local', messages: taskGenerationMessages })

const statusMeta = computed(() => generationReceiptMeta(props.job))

function resolveStepDotClass(step: TaskGenerationStep) {
  if (step.status === 'completed') return 'border-emerald-500 bg-emerald-500'
  if (step.status === 'running') return 'border-amber-500 bg-amber-500 shadow-[0_0_0_4px_rgba(245,158,11,0.18)]'
  if (step.status === 'failed') return 'border-red-500 bg-red-500'
  return 'border-slate-300 bg-white'
}

function resolveStepTextClass(step: TaskGenerationStep) {
  if (step.status === 'completed') return 'text-slate-700'
  if (step.status === 'running') return 'text-slate-900'
  if (step.status === 'failed') return 'text-red-600'
  return 'text-slate-400'
}
</script>

<template>
  <section class="rounded-2xl border border-slate-200 bg-slate-50/80 p-4 shadow-sm">
    <div class="flex items-start justify-between gap-4">
      <div class="space-y-1">
        <p class="text-sm font-semibold text-slate-900">
          {{ job.task_name || job.task?.task_name || job.job_id || t('generationOutcome.viewReceipt') }}
        </p>
        <p class="text-sm text-slate-600">
          {{ isTerminalStatus(job.status) ? t(statusMeta.helperKey) : job.message || t(statusMeta.helperKey) }}
        </p>
      </div>
      <Badge :variant="statusMeta.variant">
        {{ t(statusMeta.labelKey) }}
      </Badge>
    </div>

    <div class="mt-3 space-y-1 break-all text-xs text-slate-600">
      <p v-if="job.job_id">{{ t('generationOutcome.jobId') }}: {{ job.job_id }}</p>
      <div v-if="job.task" class="rounded-xl border border-slate-200 bg-white p-3">
        <p class="font-medium">{{ t('generationOutcome.snapshot') }}: {{ job.task.task_name }}</p>
        <p>{{ t('generationOutcome.taskId') }}: {{ job.task.id }}</p>
        <p v-if="job.task.instance_id">{{ t('generationOutcome.instanceId') }}: {{ job.task.instance_id }}</p>
      </div>
    </div>

    <div class="mt-4 grid gap-3">
      <div
        v-for="step in job.steps"
        :key="step.key"
        class="flex items-start gap-3 rounded-xl border border-white/70 bg-white px-3 py-2"
      >
        <span
          class="mt-1 h-3 w-3 shrink-0 rounded-full border-2 transition-colors"
          :class="resolveStepDotClass(step)"
        />
        <div class="min-w-0 space-y-1">
          <p class="text-sm font-medium" :class="resolveStepTextClass(step)">
            {{ step.label }}
          </p>
          <p
            v-if="step.message"
            class="text-xs"
            :class="step.status === 'failed' ? 'text-red-500' : 'text-slate-500'"
          >
            {{ step.message }}
          </p>
        </div>
      </div>
    </div>

    <p
      v-if="job.error"
      class="mt-4 rounded-xl border px-3 py-2 text-sm"
      :class="statusMeta.variant === 'destructive' ? 'border-red-200 bg-red-50 text-red-600' : 'border-amber-200 bg-amber-50 text-amber-800'"
    >
      {{ job.error }}
    </p>
  </section>
</template>
