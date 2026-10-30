/** Types mirroring the JSON the FastAPI agent returns. */

export interface SessionInfo {
  auth_required: boolean;
  username: string | null;
  expires_in?: number;
}

export interface AppConfig {
  roots: string[];
  job_types: string[];
  poll_interval: number;
  auth_required: boolean;
  initial_project: string | null;
  initial_star_file: string | null;
  /** The assistant panel is served; off unless the operator enabled it. */
  assistant_enabled?: boolean;
  /** The MCP server is mounted at /mcp. */
  mcp_enabled?: boolean;
}

export interface ProjectEntry {
  path: string;
  name: string;
  added_at: number;
  readable: boolean;
  job_count?: number;
  /** Jobs per pipeline status; absent when the project is unreadable. */
  statuses?: Record<string, number>;
  /** The highest-numbered jobs, newest first. */
  recent?: RecentJob[];
  /** How many distinct job types the project contains. */
  types?: number;
}

/** The compact job summary a project card shows. */
export interface RecentJob {
  name: string;
  alias: string;
  type_label: string;
  status: string;
  job_number: number;
}

export interface ProjectJob {
  name: string;
  alias: string;
  type_label: string;
  status: string;
  directory: string;
  job_number: number;
  outputs: string[];
}

export interface ProjectSummary {
  project_directory: string;
  pipeline_file: string;
  job_counter: number;
  n_jobs: number;
  jobs: ProjectJob[];
  nodes: { name: string; type_label: string }[];
}

export interface TiltSeriesInfo {
  name: string;
  star_file: string | null;
  tomogram_file: string | null;
  denoised_tomogram_file: string | null;
  half1_file: string | null;
  half2_file: string | null;
  pixel_size: number | null;
  n_tilt_images: number | null;
}

export interface DatasetSummary {
  star_file: string;
  n_tilt_series: number;
  tilt_series: TiltSeriesInfo[];
}

export interface TiltImage {
  index: number;
  filename: string | null;
  name: string | null;
  tilt_angle: number | null;
  defocus: number | null;
  selected: boolean;
}

export interface TiltSeriesDetail extends TiltSeriesInfo {
  images: TiltImage[];
}

export interface VolumeInfo {
  path: string;
  shape: number[];
  nx: number;
  ny: number;
  nz: number;
  mode: number;
  dtype: string;
  voxel_size: number[];
  suggested_range: [number, number];
}

export interface ExecutionHost {
  name: string;
  hostname: string;
  port: number;
  user: string | null;
  local: boolean;
  enabled: boolean;
  note: string;
  reachable?: boolean;
  problem?: string;
}

/** One GPU on a node, as nvidia-smi reports it. */
export interface NodeGpu {
  index: number;
  name: string;
  uuid: string;
  memory_used_mb: number | null;
  memory_total_mb: number | null;
  utilization_percent: number | null;
  power_watts: number | null;
  power_limit_watts: number | null;
  temperature_c: number | null;
  /** A process holds a compute context here, not just display memory. */
  has_compute_tenant?: boolean;
}

/** One RELION job running on a node, from its --pipeline_control directory. */
export interface NodeJob {
  job_directory: string;
  project: string | null;
  process_name: string | null;
  job_type: string | null;
  pids: number[];
  cpu_percent: number;
  memory_mb: number;
  gpu_memory_mb: number;
  command: string;
}

/** A live snapshot of one machine. */
export interface NodeSnapshot {
  name: string;
  reachable: boolean;
  problem: string | null;
  hostname: string;
  load_1: number | null;
  load_5: number | null;
  load_15: number | null;
  load_percent: number | null;
  cpus: number;
  memory_total_mb: number | null;
  memory_available_mb: number | null;
  memory_used_mb: number | null;
  uptime_seconds: number | null;
  gpus: NodeGpu[];
  jobs: NodeJob[];
}

export interface Job {
  id: string;
  job_type: string;
  status: string;
  command: string[];
  output_directory: string;
  started_at: number;
  finished_at: number | null;
  pid: number | null;
  returncode: number | null;
  error: string | null;
  duration: number;
  process_name: string | null;
  pipeline_status: string | null;
  /** Which machine ran it: 'local', or a name from /api/hosts. */
  host?: string;
}

export interface JobDetail extends Job {
  log: string;
  log_offset: number;
}

export interface LogChunk {
  id: string;
  chunk: string;
  next_offset: number;
  truncated: boolean;
}

/** One form control, mirroring RELION's JobOption. */
export interface JobField {
  variable: string;
  label: string;
  kind: "slider" | "boolean" | "string" | "path" | "input_node" | "radio";
  default?: string | number | boolean;
  help?: string;
  min?: number;
  max?: number;
  step?: number;
  choices?: string[];
  node_type?: string;
  pattern?: string;
  /** The web backend handles this option itself; the form does not show it. */
  internal?: boolean;
}

export interface JobGate {
  /** The boolean option whose value enables or disables the fields below. */
  controller: string;
  group: string;
  /** 'true' means the fields are live when the controller is Yes. */
  active_when: "true" | "false";
  fields: string[];
}

export interface JobTab {
  index: number;
  title: string;
  fields: string[];
  gates?: JobGate[];
}

/** One workflow-stage group of job types, from /api/job-schema. */
export interface JobCategory {
  title: string;
  job_types: string[];
}

export interface CliOption {
  program: string;
  flag: string;
  kind: "boolean" | "value";
  usage: string | null;
  default: string | null;
  required: boolean;
  hidden: boolean;
  section: string;
  /** True when a form field already controls this flag. */
  used_by_form: boolean;
}

export interface JobFormSchema {
  job_type: string;
  title: string;
  cpp_name: string;
  executable: string | null;
  subcommand: string | null;
  flag_style: "underscore" | "hyphen" | null;
  required: string[];
  fields: JobField[];
  tabs: JobTab[];
  /** Every flag the program accepts, for the advanced panel. */
  cli_options: CliOption[];
  /** Set when the program's flags cannot be read from a source file. */
  cli_options_unavailable: string | null;
  /** The desktop job this form is a branch of; equal to job_type for a whole job. */
  base_job_type: string;
  /** Which branch of that job's builder this form pins, when it is a variant. */
  branch: string | null;
}

export type JobFieldValues = Record<string, string | number | boolean>;

/** One entry of /api/project/job-types: the mapping a process label uses. */
export interface ProjectJobType {
  id: string;
  title: string;
  executable: string | null;
  process_type_label: string;
  directory: string;
  inputs: string | null;
  outputs: string[];
  /** The whole job this is a mode of; equal to `id` for a whole job. */
  base_job_type: string;
}

export interface PipelineProcess {
  name: string;
  alias: string;
  type_label: string;
  status: string;
  job_number: number;
  directory: string;
  /** The queue entry that owns this job, when it was queued here. */
  queue_entry?: string | null;
}

export interface PipelineNode {
  name: string;
  type_label: string;
  type_depth: number;
  produced_by: string | null;
  consumed_by: string[];
}

export interface PipelineGraph {
  project_directory: string;
  job_counter: number;
  processes: PipelineProcess[];
  nodes: PipelineNode[];
  edges: {
    inputs: { node: string; process: string }[];
    outputs: { process: string; node: string }[];
  };
}

/** A job waiting to run, or one that has run, in the queue. */
export interface QueueEntry {
  id: string;
  project: string;
  job_type: string;
  arguments: Record<string, unknown>;
  /** 'local' or a node name from /api/hosts. */
  host: string;
  /** Device indices requested at enqueue time. */
  gpus: number[];
  /** Cards actually held; only set while running. */
  leased_gpus: number[];
  cores: number;
  priority: number;
  exclusive: boolean;
  depends_on: string[];
  after_jobs: string[];
  after_files: string[];
  /** Upstream processes worked out from the pipeline, for display. */
  derived_dependencies: string[];
  state: "pending" | "running" | "succeeded" | "failed" | "cancelled";
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  wait_seconds: number;
  duration: number | null;
  /** The RELION job this entry started, once it has. */
  job_id: string | null;
  /** Assigned at enqueue, so output paths are known before it runs. */
  process_name: string | null;
  /** Output filename -> project-relative path, predicted at enqueue. */
  predicted_outputs: Record<string, string>;
  error: string | null;
  /** Plain-language reasons the entry has not started. */
  blocked_by: string[];
  warnings: string[];
}

/** One node's spare capacity, as the queue sees it. */
export interface QueueHost {
  host: string;
  enabled: boolean;
  cpus: number | null;
  used_cores: number;
  free_cores: number | null;
  gpus: { index: number; busy: boolean }[] | null;
  problem: string | null;
  queued: number;
}

export interface QueueSnapshot {
  entries: QueueEntry[];
  hosts: QueueHost[];
  counts: Record<
    "pending" | "running" | "succeeded" | "failed" | "cancelled",
    number
  >;
}

/** One file that may fill an input slot, from RELION's `.Nodes` listing. */
export interface InputCandidate {
  /** The pipeline node name, e.g. `Tomograms/job001/tomograms.star`. */
  node: string;
  /** The process that produces it. */
  producer: string | null;
  producer_status: string | null;
  /** The file is on disk now. */
  exists: boolean;
  /** The file has not been written yet; picking it means waiting. */
  pending: boolean;
  /** The queue entry producing it, when that job is still queued or running. */
  queue_entry: string | null;
  /** `q0003:tomograms.star`, the queue's slot spelling. Null when not queued. */
  slot: string | null;
}
