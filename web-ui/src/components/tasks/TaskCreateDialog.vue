<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { createTaskWithAI } from '@/api/tasks'
import { useTaskGenerationJob } from '@/composables/useTaskGenerationJob'
import { canRetryGeneration, generationReceiptMeta, isTerminalStatus, shouldRefreshTaskList } from '@/composables/taskGenerationState'
import { taskGenerationMessages } from '@/composables/taskGenerationMessages'
import type { TaskGenerateRequest, TaskGenerationReceipt } from '@/types/task.d.ts'
import { parseTaskFormDefaults } from '@/lib/taskFormQuery'
import TaskForm from '@/components/tasks/TaskForm.vue'
import TaskGenerationDialog from '@/components/tasks/TaskGenerationDialog.vue'
import { Button } from '@/components/ui/button'
import { toast } from '@/components/ui/toast'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
const { t } = useI18n({ useScope: 'local', messages: taskGenerationMessages })

const props = defineProps<{
  accountOptions?: { name: string; path: string }[]
}>()

const emit = defineEmits<{
  (event: 'created'): void
}>()

const route = useRoute()
const isFormOpen = ref(false)
const isProgressOpen = ref(false)
const isSubmitting = ref(false)
// A transport error is not evidence that the server did not create the task.
const submissionUncertain = ref(false)
const synchronousReceipt = ref<TaskGenerationReceipt | null>(null)
const defaultAccountPath = ref('')
const defaultValues = ref({})
const {
  activeJob,
  pollingError,
  beginPolling,
  clearJob,
} = useTaskGenerationJob()

const activeReceipt = computed(() => synchronousReceipt.value ?? activeJob.value)
const submissionBlocked = computed(() => isSubmitting.value || submissionUncertain.value || (activeReceipt.value !== null && !canRetryGeneration(activeReceipt.value)))

function openCreateForm() {
  if (activeReceipt.value) {
    isProgressOpen.value = true
    return
  }
  isFormOpen.value = true
}

function retryGeneration() {
  if (!canRetryGeneration(activeReceipt.value) || isSubmitting.value) return
  isProgressOpen.value = false
  isFormOpen.value = true
}

function resolveAccountPath(accountName: string) {
  const match = (props.accountOptions || []).find((account) => account.name === accountName)
  return match ? match.path : ''
}

async function handleCreateTask(data: TaskGenerateRequest) {
  if (submissionBlocked.value) return
  isSubmitting.value = true
  clearJob()
  synchronousReceipt.value = null
  try {
    const result = await createTaskWithAI(data)
    // Top-level terminal outcomes take precedence over the task snapshot. A
    // saved keyword task can still require repair; it is not full success.
    if (result.status && isTerminalStatus(result.status) && result.status !== 'completed') {
      synchronousReceipt.value = { ...result, status: result.status }
      isFormOpen.value = false
      isProgressOpen.value = true
      return
    }
    if (result.job) {
      isFormOpen.value = false
      isProgressOpen.value = true
      beginPolling(result.job)
      isSubmitting.value = false
      return
    }
    if (!result.task) throw new Error('Missing task generation receipt')
    emit('created')
    toast({ title: t('tasks.toasts.created') })
    isFormOpen.value = false
  } catch {
    submissionUncertain.value = true
    emit('created')
    toast({
      title: t('generationOutcome.status.unknown'),
      description: t('generationOutcome.helper.unknown'),
    })
  } finally {
    isSubmitting.value = false
  }
}

watch(
  () => [route.query, props.accountOptions],
  () => {
    const accountName = typeof route.query.account === 'string' ? route.query.account : ''
    defaultAccountPath.value = accountName ? resolveAccountPath(accountName) : ''
    defaultValues.value = parseTaskFormDefaults(route.query)
    if (route.query.create === '1') {
      isFormOpen.value = true
    }
  },
  { immediate: true }
)

watch(
  activeReceipt,
  (job, previousJob) => {
    if (!job || !isTerminalStatus(job.status)) return
    if (job.job_id && job.job_id === previousJob?.job_id && job.status === previousJob.status) return
    isSubmitting.value = false
    // The parent uses this event to reload the list; it is not a success claim.
    if (shouldRefreshTaskList(job)) emit('created')
    if (job.status === 'completed') {
      toast({ title: t('tasks.toasts.created') })
      isProgressOpen.value = false
      clearJob()
      return
    }
    isProgressOpen.value = true
    const meta = generationReceiptMeta(job)
    if (meta.variant === 'destructive') {
      toast({
        title: t('tasks.toasts.createFailed'),
        description: job.error || job.message,
        variant: 'destructive',
      })
      return
    }
    toast({ title: t(meta.labelKey), description: t(meta.helperKey) })
  }
)

watch(pollingError, (value) => {
  if (!value) return
  isSubmitting.value = false
  toast({
    title: t('tasks.toasts.progressFailed'),
    description: value.message,
    variant: 'destructive',
  })
})
</script>

<template>
  <Dialog v-model:open="isFormOpen">
    <Button :disabled="isSubmitting" @click="openCreateForm">
      {{ activeReceipt ? t('generationOutcome.viewReceipt') : t('tasks.createDialog.trigger') }}
    </Button>
    <DialogContent class="sm:max-w-[640px] max-h-[85vh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>{{ t('tasks.createDialog.title') }}</DialogTitle>
      </DialogHeader>
      <p v-if="submissionUncertain" role="alert" class="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
        {{ t('generationOutcome.status.unknown') }}: {{ t('generationOutcome.helper.unknown') }}
      </p>
      <TaskForm
        mode="create"
        :account-options="accountOptions"
        :default-account="defaultAccountPath"
        :default-values="defaultValues"
        @submit="(data) => handleCreateTask(data as TaskGenerateRequest)"
      />
      <DialogFooter>
        <Button type="submit" form="task-form" :disabled="submissionBlocked">
          {{ isSubmitting ? t('tasks.createDialog.submitting') : t('tasks.createDialog.submit') }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
  <TaskGenerationDialog
    v-model:open="isProgressOpen"
    :job="activeReceipt"
    @retry="retryGeneration"
  />
</template>
