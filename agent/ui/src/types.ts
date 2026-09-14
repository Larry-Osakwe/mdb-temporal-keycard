export interface StartResponse {
  workflow_id: string;
}

export interface ProgressResponse {
  workflow_id: string;
  status?: string;
  steps: string[];
  tool_calls: string[];
  answer: string | null;
  model: string | null;
  done: boolean;
  /** Keycard policy denials the workflow recorded (one per refused tool call). */
  denials?: string[];
}

export interface AccessState {
  application: string;
  resource: string;
  /** null when the zone did not report the dependency list. */
  allowed: boolean | null;
}
