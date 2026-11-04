// Based on the Pydantic model in the backend

export interface Task {
  id: number;
  task_name: string;
  enabled: boolean;
  keyword: string;
  description: string;
  analyze_images: boolean;
  max_pages: number;
  personal_only: boolean;
  min_price: string | null;
  max_price: string | null;
  cron: string | null;
  next_run_at?: string | null;
  ai_prompt_base_file: string;
  ai_prompt_criteria_file: string;
  account_state_file?: string | null;
  account_strategy: 'auto' | 'fixed' | 'rotate' | 'ass';
  free_shipping?: boolean;
  new_publish_option?: string | null;
  region?: string | null;
  decision_mode: 'ai' | 'keyword';
  keyword_rules: string[];
  is_running: boolean;
}

export type TaskGenerationStatus = 'queued' | 'running' | 'completed' | 'failed' | 'partial_success' | 'unknown';
export type TaskGenerationStepStatus = 'pending' | 'running' | 'completed' | 'failed';

export interface TaskGenerationStep {
  key: string;
  label: string;
  status: TaskGenerationStepStatus;
  message: string;
}

// Durable receipts contain the creation-time model, not a live task lookup.
export interface GeneratedTaskSnapshot extends Omit<Task, 'id' | 'description'> {
  id: number | null;
  description?: string | null;
  instance_id?: string;
  config_version?: number;
}

// Synchronous keyword receipts do not have a server-side generation job.
export interface TaskGenerationReceipt {
  job_id?: string;
  status: TaskGenerationStatus;
  // Restart/missing-history receipts may only contain job_id/status/retry_safe.
  task_name?: string;
  message?: string;
  current_step?: string | null;
  steps?: TaskGenerationStep[];
  task?: GeneratedTaskSnapshot | null;
  error?: string | null;
  task_committed?: boolean | null;
  retry_safe?: boolean;
}

export interface TaskGenerationJob extends TaskGenerationReceipt {
  job_id: string;
}

export interface TaskCreateResponse extends Partial<TaskGenerationReceipt> {
  job?: TaskGenerationJob;
}

// For PATCH requests, all fields are optional
export type TaskUpdate = Partial<Omit<Task, 'id' | 'next_run_at'>>;

// For task creation
export interface TaskGenerateRequest {
  task_name: string;
  keyword: string;
  description?: string;
  analyze_images?: boolean;
  personal_only?: boolean;
  min_price?: string | null;
  max_price?: string | null;
  max_pages?: number;
  cron?: string | null;
  account_state_file?: string | null;
  account_strategy?: 'auto' | 'fixed' | 'rotate' | 'ass';
  free_shipping?: boolean;
  new_publish_option?: string | null;
  region?: string | null;
  decision_mode?: 'ai' | 'keyword';
  keyword_rules?: string[];
}
