/**
 * Cluster and queue: RELION's execution hosts, their live load, and the
 * native job queue.
 *
 * Placed as a full-page Settings section, which is where this app puts
 * environment-level configuration and monitoring (deployment, credentials and
 * runtime services live there for the same reason). Sources:
 *
 *   GET /api/hosts  - the host list and which one is default
 *   GET /api/nodes  - one sampling per host: load, memory, GPUs, jobs running
 *   GET /api/queue  - pending/running entries plus per-host capacity
 *
 * Styling is this app's own vocabulary - `settings-row`, `border-b
 * border-border`, `status-pill`, `text-muted-foreground` - so the page reads as
 * part of the settings surface rather than a bolted-on dashboard.
 */
import { useCallback, useEffect, useState } from 'react';
import { Cpu, HardDrive, Loader2, MonitorPlay, RefreshCw, Server, Trash2 } from 'lucide-react';

import { api } from '../lib/api';
import { tx } from '../lib/i18n';
import { Button } from './ui/button';

interface Gpu {
  index: number;
  name: string;
  memory_used_mb: number;
  memory_total_mb: number;
  utilization_percent: number;
  temperature_c: number | null;
  power_watts: number | null;
  has_compute_tenant?: boolean;
}

interface NodeSample {
  name: string;
  reachable: boolean;
  problem: string | null;
  hostname: string;
  load_percent: number;
  cpus: number;
  memory_used_mb: number;
  memory_total_mb: number;
  gpus: Gpu[];
  jobs: Array<{ name?: string; process?: string; project?: string }>;
}

interface HostEntry {
  name: string;
  hostname: string;
  port: number;
  user: string | null;
  local: boolean;
  enabled: boolean;
  note: string;
}

interface QueueEntry {
  id: string;
  job_type: string;
  host: string;
  gpus: number[];
  leased_gpus: number[];
  exclusive: boolean;
  state: string;
  blocked_reason?: string | null;
  error?: string | null;
  project: string;
}

interface QueueSnapshot {
  entries: QueueEntry[];
  hosts: Array<{
    host: string;
    enabled: boolean;
    cpus: number;
    free_cores: number;
    gpus: Array<{ index: number; busy: boolean }>;
    problem: string | null;
  }>;
  counts: Record<string, number>;
}

const REFRESH_MS = 5000;

function percent(used: number, total: number): number {
  return total > 0 ? Math.round((used / total) * 100) : 0;
}

/** A thin bar, tinted by saturation: quiet until something is actually loaded. */
function Meter({ value }: { value: number }) {
  return (
    <span
      className="inline-block h-1 w-20 overflow-hidden rounded-full bg-border align-middle"
      role="meter"
      aria-valuenow={value}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <span
        className="block h-full rounded-full"
        style={{
          width: `${Math.min(100, Math.max(0, value))}%`,
          background: value >= 90 ? 'var(--destructive)' : 'var(--foreground)',
          opacity: value >= 90 ? 1 : 0.45,
        }}
      />
    </span>
  );
}

export function ClusterPage({ active }: { active: boolean }) {
  const [hosts, setHosts] = useState<HostEntry[]>([]);
  const [nodes, setNodes] = useState<NodeSample[]>([]);
  const [queue, setQueue] = useState<QueueSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Addresses, one per line: a deployment is a set of identical machines, and
  // the agent provisions each one from the login it already has.
  const [addresses, setAddresses] = useState('');
  const [provisioning, setProvisioning] = useState(false);
  const [progress, setProgress] = useState<
    Array<{ host: string; name: string; ok: boolean; steps: string[]; error?: string | null }>
  >([]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [hostList, nodeList, queueList] = await Promise.all([
        api<{ hosts: HostEntry[] }>('/api/hosts'),
        api<{ nodes: NodeSample[] }>('/api/nodes'),
        api<QueueSnapshot>('/api/queue'),
      ]);
      setHosts(hostList.hosts);
      setNodes(nodeList.nodes);
      setQueue(queueList);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll only while the section is on screen: a settings page nobody is looking
  // at should not keep a 5 s ssh round trip going.
  useEffect(() => {
    if (!active) return;
    void refresh();
    const timer = setInterval(() => void refresh(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [active, refresh]);

  async function toggleHost(host: HostEntry) {
    try {
      await api(`/api/hosts/${encodeURIComponent(host.name)}`, 'PATCH', {
        enabled: !host.enabled,
      });
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function removeHost(host: HostEntry) {
    try {
      await api(`/api/hosts/${encodeURIComponent(host.name)}`, 'DELETE');
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function addHosts() {
    if (!addresses.trim() || provisioning) return;
    setProvisioning(true);
    setProgress([]);
    setError(null);
    try {
      const result = await api<{
        results: Array<{
          host: string;
          name: string;
          ok: boolean;
          steps: string[];
          error?: string | null;
        }>;
        added: number;
        failed: number;
      }>('/api/hosts/provision', 'POST', { addresses });
      setProgress(result.results);
      // Only clear the field on a clean run: a partial failure leaves the text
      // so the operator can fix one address and retry, rather than retyping.
      if (!result.failed) setAddresses('');
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setProvisioning(false);
    }
  }

  async function cancelEntry(entry: QueueEntry) {
    try {
      await api(`/api/queue/${encodeURIComponent(entry.id)}`, 'DELETE');
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  const counts = queue?.counts ?? {};

  return (
    <div>
      <div className="mb-4 flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">
          {tx('每 5 秒刷新一次；指标经 ssh 从各节点读取。')}
        </p>
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={loading}>
          <RefreshCw className={loading ? 'animate-spin' : ''} />
          {tx('刷新')}
        </Button>
      </div>

      {error && (
        <p role="alert" className="mb-4 text-xs text-destructive">
          {error}
        </p>
      )}

      {/* ---------------------------------------------------------- queue */}
      <section className="mb-8">
        <div className="flex items-baseline justify-between border-b border-border pb-2">
          <h2 className="text-sm font-medium">{tx('队列')}</h2>
          <span className="text-xs text-muted-foreground">
            {Object.entries(counts)
              .filter(([, value]) => value > 0)
              .map(([key, value]) => `${value} ${key}`)
              .join(' · ') || tx('空闲')}
          </span>
        </div>

        {(queue?.entries.length ?? 0) === 0 ? (
          <p className="py-6 text-sm text-muted-foreground">
            {tx('队列为空。在画布上点 Run 会把作业放进这里。')}
          </p>
        ) : (
          queue?.entries.map((entry) => (
            <div
              key={entry.id}
              className="flex items-center gap-4 border-b border-border py-3 text-sm"
            >
              <span className="w-16 font-mono text-xs text-muted-foreground">{entry.id}</span>
              <span className="min-w-0 flex-1 truncate">{entry.job_type}</span>
              <span className="shrink-0 text-xs text-muted-foreground">{entry.host}</span>
              <span className="w-24 shrink-0 text-xs text-muted-foreground">
                {entry.gpus.length
                  ? `${tx('GPU')} ${entry.leased_gpus.length ? entry.leased_gpus.join(',') : entry.gpus.join(',')}`
                  : tx('自动 GPU')}
                {entry.exclusive ? ` · ${tx('整机')}` : ''}
              </span>
              <span
                className="status-pill"
                title={entry.error ?? entry.blocked_reason ?? undefined}
              >
                {entry.state}
              </span>
              {entry.state === 'pending' || entry.state === 'running' ? (
                <Button variant="ghost" size="sm" onClick={() => void cancelEntry(entry)}>
                  <Trash2 />
                </Button>
              ) : (
                <span className="w-8" />
              )}
            </div>
          ))
        )}
      </section>

      {/* ---------------------------------------------------------- hosts */}
      <section className="mb-8">
        <h2 className="border-b border-border pb-2 text-sm font-medium">
          {tx('执行节点')}
        </h2>

        {nodes.map((node) => (
          <div key={node.name} className="border-b border-border py-4">
            <div className="flex items-center gap-3">
              <Server size={15} className="shrink-0 text-muted-foreground" />
              <span className="font-medium">{node.name}</span>
              <span className="text-xs text-muted-foreground">{node.hostname}</span>
              <span className="flex-1" />
              <span className="status-pill">
                {node.reachable ? tx('在线') : tx('不可达')}
              </span>
              {(() => {
                const host = hosts.find((candidate) => candidate.name === node.name);
                if (!host || host.local) return null;
                return (
                  <>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => void toggleHost(host)}
                      title={host.enabled ? tx('停用') : tx('启用')}
                    >
                      {host.enabled ? tx('停用') : tx('启用')}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => void removeHost(host)}>
                      <Trash2 />
                    </Button>
                  </>
                );
              })()}
            </div>

            {node.problem && (
              <p className="mt-1 pl-7 text-xs text-destructive">{node.problem}</p>
            )}

            <div className="mt-2 grid gap-x-8 gap-y-1 pl-7 text-xs text-muted-foreground sm:grid-cols-2">
              <span className="flex items-center gap-2">
                <Cpu size={13} />
                {tx('负载')} <Meter value={node.load_percent} /> {node.load_percent}%
                <span className="text-muted-foreground/70">
                  ({node.cpus} {tx('核')})
                </span>
              </span>
              <span className="flex items-center gap-2">
                <HardDrive size={13} />
                {tx('内存')}{' '}
                <Meter value={percent(node.memory_used_mb, node.memory_total_mb)} />{' '}
                {Math.round(node.memory_used_mb / 1024)} /{' '}
                {Math.round(node.memory_total_mb / 1024)} GB
              </span>
            </div>

            {node.gpus.length > 0 && (
              <div className="mt-2 space-y-1 pl-7">
                {node.gpus.map((gpu) => (
                  <div key={gpu.index} className="flex items-center gap-2 text-xs">
                    <MonitorPlay size={13} className="shrink-0 text-muted-foreground" />
                    <span className="w-6 font-mono text-muted-foreground">{gpu.index}</span>
                    <span className="min-w-0 flex-1 truncate">{gpu.name}</span>
                    <span
                      className="w-4 text-center"
                      title={gpu.has_compute_tenant ? tx('有进程占用') : tx('空闲')}
                    >
                      {gpu.has_compute_tenant ? '●' : '○'}
                    </span>
                    <Meter value={gpu.utilization_percent} />
                    <span className="w-24 text-right text-muted-foreground">
                      {Math.round(gpu.memory_used_mb / 1024)}/
                      {Math.round(gpu.memory_total_mb / 1024)} GB
                    </span>
                    <span className="w-12 text-right text-muted-foreground">
                      {gpu.temperature_c != null ? `${Math.round(gpu.temperature_c)}°C` : '—'}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {node.jobs.length > 0 && (
              <p className="mt-2 pl-7 text-xs text-muted-foreground">
                {tx('运行中的作业')}: {node.jobs.length}
              </p>
            )}
          </div>
        ))}

        {nodes.length === 0 && !error && (
          <p className="py-6 text-sm text-muted-foreground">{tx('正在读取节点指标…')}</p>
        )}
      </section>

      {/* --------------------------------------------------------- add host */}
      <section>
        <h2 className="border-b border-border pb-2 text-sm font-medium">
          {tx('添加 GPU 节点')}
        </h2>
        <div className="mt-4 max-w-[560px] space-y-2">
          <textarea
            className="input min-h-20 w-full py-2 text-sm"
            aria-label={tx('节点地址')}
            placeholder={'10.0.0.11\n10.0.0.12\n10.0.0.13'}
            value={addresses}
            onChange={(event) => setAddresses(event.target.value)}
            disabled={provisioning}
          />
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => void addHosts()}
              disabled={!addresses.trim() || provisioning}
            >
              {provisioning ? <Loader2 className="animate-spin" /> : <Server />}
              {provisioning ? tx('正在配置…') : tx('添加并配置')}
            </Button>
            <span className="text-xs text-muted-foreground">
              {tx('每行一个地址，也接受逗号或空格分隔。')}
            </span>
          </div>
        </div>
        <p className="mt-2 max-w-[560px] text-xs leading-relaxed text-muted-foreground">
          {tx('使用当前登录的账户和密码，为每个节点安装 agent 专属密钥（限定为只能执行作业命令），然后验证免密访问。密码只用于这一次操作。')}
        </p>
        {progress.length > 0 && (
          <ul className="mt-3 max-w-[560px] space-y-1 text-xs">
            {progress.map((item) => (
              <li key={item.host} className="flex items-start gap-2">
                <span
                  className={
                    item.ok ? 'text-muted-foreground' : 'text-destructive'
                  }
                  aria-hidden="true"
                >
                  {item.ok ? '✓' : '✕'}
                </span>
                <span className="min-w-0">
                  <span className="font-mono">{item.host}</span>
                  {item.ok ? (
                    <span className="ml-2 text-muted-foreground">
                      {tx('已就绪（{0}）', [item.steps.length])}
                    </span>
                  ) : (
                    <span className="ml-2 text-destructive">{item.error}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
