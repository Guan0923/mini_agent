import type { BenchmarkResult, SkillInfo, TaskInfo, ToolInfo } from "../types";
import { jsonBody, requestJson } from "./transport/request";

export async function listTasks(): Promise<TaskInfo[]> {
  return requestJson<TaskInfo[]>("/benchmark/tasks");
}

export async function runBenchmark(task: string, planner: string): Promise<BenchmarkResult> {
  return requestJson<BenchmarkResult>("/benchmark/run", jsonBody({ task, planner }));
}

export async function runAllBenchmark(planner: string): Promise<BenchmarkResult[]> {
  return requestJson<BenchmarkResult[]>("/benchmark/run-all", jsonBody({ planner }));
}

export async function listTools(): Promise<ToolInfo[]> {
  return requestJson<ToolInfo[]>("/api/tools");
}

export async function listSkills(): Promise<SkillInfo[]> {
  return requestJson<SkillInfo[]>("/api/skills");
}
