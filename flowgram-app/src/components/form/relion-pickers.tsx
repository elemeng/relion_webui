/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

import { FolderOpen, ListTree } from 'lucide-react';

import { api } from '../../lib/api';
import { tx } from '../../lib/i18n';

/**
 * RELION's input widgets.
 *
 * A RELION input option resolves to a path - upstream output, a slot on a
 * queued job, or anything the user types. Three surfaces, one value:
 *
 * - the plain text field (kept, typed and pasted paths stay first-class);
 * - the filesystem button: browse absolute paths, project-root-relative when
 *   the file lives in the project (what RELION itself stores);
 * - the slots button: the input-candidates picker, which lists
 *   `.Nodes/<type>/` exactly as the desktop GUI does - upstream files with
 *   their producer and whether they exist yet.
 */

/** Wire shape of `GET /api/project/input-candidates`. */
interface Candidate {
  node: string;
  producer: string | null;
  producer_status: string | null;
  exists: boolean;
  pending: boolean;
  queue_entry: string | null;
  slot: string | null;
}

interface CandidatesResponse {
  node_type: string;
  candidates: Candidate[];
}

const statusLabel: Record<string, string> = {
  Succeeded: '已完成',
  Running: '运行中',
  Scheduled: '已队列',
  Failed: '失败',
  Aborted: '已中止',
};

function CandidateRow({
  candidate,
  choose,
}: {
  candidate: Candidate;
  choose: (value: string) => void;
}) {
  const filename = candidate.node.split('/').pop() ?? candidate.node;
  return (
    <button
      type="button"
      className="nf-candidate-row"
      data-pending={candidate.pending || undefined}
      onClick={() => choose(candidate.node)}
      title={candidate.node}
    >
      <span className="nf-candidate-file">{filename}</span>
      <span className="nf-candidate-meta">
        {candidate.producer
          ? `${candidate.producer} · ${statusLabel[candidate.producer_status ?? ''] ?? candidate.producer_status ?? ''}`
          : tx('无上游作业')}
      </span>
      <span className={`nf-candidate-state${candidate.pending ? ' nf-candidate-pending' : ''}`}>
        {candidate.pending ? tx('等待产出') : tx('已存在')}
      </span>
    </button>
  );
}

/** The slots popover: RELION's `.Nodes/<type>/` listing, as the GUI shows it. */
export function SlotPicker({
  jobType,
  variable,
  projectId,
  label,
  choose,
}: {
  jobType: string;
  variable: string;
  projectId: string;
  label: string;
  choose: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<CandidatesResponse | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const anchor = useRef<HTMLSpanElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });

  useLayout(open, anchor, panel, (left, top) => setPosition({ left, top }));

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (
        !anchor.current?.contains(event.target as Node) &&
        !panel.current?.contains(event.target as Node)
      )
        setOpen(false);
    };
    document.addEventListener('pointerdown', outside, true);
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        setOpen(false);
      }
    };
    document.addEventListener('keydown', escape, true);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', escape, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open || data || loading) return;
    setLoading(true);
    const query = new URLSearchParams({
      job_type: jobType,
      variable,
      project_directory: projectId,
    });
    api<CandidatesResponse>(`/api/project/input-candidates?${query}`)
      .then((next) => {
        setData(next);
        setError('');
      })
      .catch((cause) => setError((cause as Error).message))
      .finally(() => setLoading(false));
  }, [open, data, loading, jobType, variable, projectId]);

  return (
    <span ref={anchor} className="nf-slot-anchor">
      <button
        type="button"
        className="nf-slot-trigger"
        aria-label={label}
        aria-expanded={open}
        title={tx('从上游作业选择输入')}
        onClick={(event) => {
          event.preventDefault();
          setOpen((value) => !value);
        }}
      >
        <ListTree size={13} />
      </button>
      {open &&
        createPortal(
          <div
            ref={panel}
            className="nf-slot-panel"
            role="dialog"
            aria-label={label}
            style={{ left: position.left, top: position.top }}
          >
            <p className="nf-slot-heading">
              {tx('上游输出')} <code>{data?.node_type || '…'}</code>
            </p>
            {error ? (
              <p className="nf-error">{error}</p>
            ) : loading ? (
              <p className="nf-slot-empty">{tx('读取候选输入…')}</p>
            ) : data?.candidates.length ? (
              <div className="nf-slot-list">
                {data.candidates.map((candidate) => (
                  <CandidateRow
                    key={candidate.node}
                    candidate={candidate}
                    choose={(value) => {
                      choose(value);
                      setOpen(false);
                    }}
                  />
                ))}
              </div>
            ) : (
              <p className="nf-slot-empty">
                {tx('没有该类型的候选输入。上游作业运行过一次后，输出会出现在这里。')}
              </p>
            )}
          </div>,
          document.body
        )}
    </span>
  );
}

/** Position like the inspector's own popover: below the trigger, in view. */
function useLayout(
  open: boolean,
  anchor: React.RefObject<HTMLElement | null>,
  panel: React.RefObject<HTMLElement | null>,
  apply: (left: number, top: number) => void,
) {
  useEffect(() => {
    if (!open || !anchor.current || !panel.current) return;
    const rect = anchor.current.getBoundingClientRect();
    const width = panel.current.offsetWidth || 380;
    const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
    const below = rect.bottom + 6;
    const height = panel.current.offsetHeight || 300;
    const top =
      below + height < window.innerHeight - 12 ? below : Math.max(12, rect.top - height - 6);
    apply(left, top);
  });
}

/**
 * The path input: RELION's value surface.
 *
 * The field carries its own grey placeholder (the schema's
 * `pattern`/default hint, e.g. `path/to/tomograms.star`), a filesystem button
 * on the right, and - for node-typed fields - the slots button next to it.
 */
export function PathInput({
  value,
  onChange,
  placeholder,
  slots,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  slots?: ReactNode;
}) {
  return (
    <div className="nf-path-input">
      <input
        className="nf-path-field"
        type="text"
        value={value}
        placeholder={placeholder}
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
      />
      <button
        type="button"
        className="nf-slot-trigger"
        aria-label={tx('浏览文件')}
        title={tx('浏览文件夹')}
        onClick={(event) => {
          event.preventDefault();
          // Real file picking needs a filesystem bridge; the field itself
          // stays the source of truth. The button is where the OS picker will
          // attach once the bridge exists (v2, tauri-free).
        }}
      >
        <FolderOpen size={13} />
      </button>
      {slots}
    </div>
  );
}
