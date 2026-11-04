<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { TaskGenerationReceipt } from '@/types/task.d.ts'
import { canRetryGeneration, isTerminalStatus } from '@/composables/taskGenerationState'
import { taskGenerationMessages } from '@/composables/taskGenerationMessages'
import TaskGenerationProgress from '@/components/tasks/TaskGenerationProgress.vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
const { t } = useI18n({ useScope: 'local', messages: taskGenerationMessages })

const props = defineProps<{
  job: TaskGenerationReceipt | null
  open: boolean
}>()

const emit = defineEmits<{
  (event: 'update:open', value: boolean): void
  (event: 'retry'): void
}>()

const isRunning = computed(() => {
  if (!props.job) return false
  return !isTerminalStatus(props.job.status)
})

const helperText = computed(() => {
  if (!isRunning.value) return ''
  return t('tasks.generation.helperRunning')
})
</script>

<template>
  <Dialog :open="open" @update:open="(value) => emit('update:open', value)">
    <DialogContent class="sm:max-w-[560px]">
      <DialogHeader>
        <DialogTitle>{{ t('tasks.generation.title') }}</DialogTitle>
        <DialogDescription>
          {{ isRunning ? t('tasks.generation.description') : t('generationOutcome.receiptDescription') }}
        </DialogDescription>
      </DialogHeader>

      <TaskGenerationProgress v-if="job" :job="job" />
      <p v-if="helperText" class="text-xs text-slate-500">
        {{ helperText }}
      </p>

      <DialogFooter>
        <Button v-if="canRetryGeneration(job)" @click="emit('retry')">
          {{ t('generationOutcome.retry') }}
        </Button>
        <Button variant="outline" @click="emit('update:open', false)">
          {{ isRunning ? t('tasks.generation.closeWindow') : t('common.close') }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
