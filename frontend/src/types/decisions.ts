export interface DecisionOption {
  label: string;
  description: string;
}

export interface DecisionQuestion {
  id: string;
  header?: string;
  question: string;
  options: DecisionOption[];
}

export interface DecisionRequest {
  decision_id: string;
  kind: "tool" | "plan" | "question" | "resume" | "skill";
  message?: string;
  tool?: string;
  arguments?: Record<string, unknown> | string;
  plan?: string;
  goal?: string;
  steps?: string[];
  details?: string;
  questions?: DecisionQuestion[];
  // Skill trust review (kind === "skill").
  skill?: string;
  description?: string;
  project_id?: string;
  workspace_sha256?: string;
  tree_sha256?: string;
  path?: string;
}
