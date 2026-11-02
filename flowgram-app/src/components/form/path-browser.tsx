/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  ArrowUp,
  Check,
  ChevronRight,
  Eye,
  EyeOff,
  FileText,
  Folder,
  FolderOpen,
  HardDrive,
  Home,
  Loader2,
} from 'lucide-react';

import { api } from '../../lib/api';
import { tx } from '../../lib/i18n';
import { Button } from '../ui/button';
import { Dialog } from '../ui/dialog';
import { Input } from '../ui/input';

/** Wire shapes of the agent's `/api/browse` (see backend browse.py). */
interface Entry {
  name: string;
  path: string;
  is_dir: boolean;
  is_project: boolean;
  is_star: boolean;
  size?: number;
  error?: string;
}

interface Listing {
  path: string;
  parent: string | null;
  entries: Entry[];
  truncated: boolean;
  error?: string;
}

interface Location {
  label: string;
  path: string;
}

const sizeLabel = (size?: number) => {
  if (size == null) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${Math.round(size / 1024)} KB`;
  if (size < 1024 ** 3) return `${Math.round(size / 1024 ** 2)} MB`;
  return `${Math.round(size / 1024 ** 3)} GB`;
};

/**
 * The agent's filesystem, as a picker.
 *
 * Browsing happens on the machine running RELION, not in the browser - the
 * agent's `/api/browse` is the one filesystem view both this app and the main
 * web UI share, so a path chosen here is a path RELION can open.
 *
 * RELION stores most inputs project-relative (`Denoise/job006/tomograms.star`).
 * The picker therefore resolves a relative value against the project root when
 * opening and writes back relative when the picked path lives inside it, so a
 * hand-typed and a browsed value look the same in the pipeline.
 */
export function PathBrowser({
  open,
  onOpenChange,
  onPick,
  value,
  projectId,
  filter = 'all',
  title,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPick: (path: string) => void;
  /** The field's current value; a relative path is resolved against projectId. */
  value?: string;
  projectId?: string;
  filter?: 'all' | 'directory' | 'star';
  title?: string;
}) {
  const [listing, setListing] = useState<Listing | null>(null);
  const [locations, setLocations] = useState<Location[]>([]);
  const [typed, setTyped] = useState('');
  const [selected, setSelected] = useState<Entry | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [showHidden, setShowHidden] = useState(false);
  const request = useRef(0);

  const toAbsolute = useCallback(
    (path: string | undefined) => {
      if (!path) return projectId;
      if (path.startsWith('/')) return path;
      if (!projectId) return path;
      return `${projectId.replace(/\/$/, '')}/${path}`;
    },
    [projectId],
  );

  const toStored = useCallback(
    (path: string) => {
      if (!projectId) return path;
      const root = projectId.replace(/\/$/, '');
      return path.startsWith(`${root}/`) ? path.slice(root.length + 1) : path;
    },
    [projectId],
  );

  const load = useCallback(
    async (path?: string) => {
      const generation = ++request.current;
      setLoading(true);
      setError('');
      setSelected(null);
      try {
        const params = new URLSearchParams({ filter });
        if (path) params.set('path', path);
        if (showHidden) params.set('hidden', 'true');
        const data = await api<Listing>(`/api/browse?${params}`);
        if (generation !== request.current) return;
        setListing(data);
        setTyped(data.path);
        if (data.error) setError(data.error);
      } catch (cause) {
        if (generation === request.current) setError((cause as Error).message);
      } finally {
        if (generation === request.current) setLoading(false);
      }
    },
    [filter, showHidden],
  );

  // Opening starts at the value's directory if there is one, else the project.
  useEffect(() => {
    if (!open) return;
    const absolute = toAbsolute(value || undefined);
    // A value names a file (or a directory): open in its directory. With no
    // value the project root is the useful place to start - it is where every
    // RELION input lives, and it is what a relative path is relative to.
    const guess = value
      ? absolute?.replace(/\/[^/]*$/, '') || absolute
      : projectId || absolute;
    void load(guess);
    api<{ locations: Location[] }>('/api/browse/locations')
      .then((data) => setLocations(data.locations))
      .catch(() => setLocations([]));
    // `value` is read once per opening; reopening with a new value re-runs it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, showHidden]);

  const choose = () => {
    if (!listing) return;
    const picked = selected?.is_dir ? selected.path : selected?.path || listing.path;
    onPick(toStored(picked));
    onOpenChange(false);
  };

  const crumbs = useMemo(() => {
    if (!listing) return [];
    const parts = listing.path.split('/').filter(Boolean);
    return parts.map((part, index) => ({
      name: part,
      path: `/${parts.slice(0, index + 1).join('/')}`,
    }));
  }, [listing]);

  const files = listing?.entries ?? [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={title ?? tx('选择路径')}>
      <div className="nf-browser">
        <div className="nf-browser-bar">
          <Button
            variant="ghost"
            size="icon"
            aria-label={tx('上级目录')}
            disabled={!listing?.parent}
            onClick={() => listing?.parent && void load(listing.parent)}
          >
            <ArrowUp />
          </Button>
          <Input
            aria-label={tx('路径')}
            value={typed}
            spellCheck={false}
            onChange={(event) => setTyped(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                void load(typed.trim() || undefined);
              }
            }}
          />
          <Button
            variant="ghost"
            size="icon"
            aria-label={showHidden ? tx('隐藏隐藏文件') : tx('显示隐藏文件')}
            aria-pressed={showHidden}
            onClick={() => setShowHidden((current) => !current)}
          >
            {showHidden ? <Eye /> : <EyeOff />}
          </Button>
        </div>

        <div className="nf-browser-crumbs" aria-label={tx('当前位置')}>
          {crumbs.map((crumb, index) => (
            <span key={crumb.path} className="nf-browser-crumb">
              {index > 0 && <ChevronRight size={11} />}
              <button type="button" onClick={() => void load(crumb.path)}>
                {crumb.name}
              </button>
            </span>
          ))}
        </div>

        {locations.length > 0 && (
          <div className="nf-browser-locations">
            {locations.map((location) => (
              <button
                key={location.path}
                type="button"
                title={location.path}
                onClick={() => void load(location.path)}
              >
                {location.label === 'home' ? <Home size={12} /> : <HardDrive size={12} />}
                {location.label === 'home' ? tx('主目录') : tx('当前项目')}
              </button>
            ))}
          </div>
        )}

        <div className="nf-browser-list" role="listbox" aria-label={tx('目录内容')}>
          {loading ? (
            <p className="nf-browser-empty">
              <Loader2 size={13} className="animate-spin" /> {tx('读取目录…')}
            </p>
          ) : error ? (
            <p className="nf-browser-empty nf-error">{error}</p>
          ) : files.length ? (
            files.map((entry) => (
              <button
                key={entry.path}
                type="button"
                role="option"
                aria-selected={selected?.path === entry.path}
                className="nf-browser-row"
                data-selected={selected?.path === entry.path || undefined}
                onDoubleClick={() => {
                  if (entry.is_dir) void load(entry.path);
                  else {
                    setSelected(entry);
                    onPick(toStored(entry.path));
                    onOpenChange(false);
                  }
                }}
                onClick={() => setSelected(entry)}
                title={entry.path}
              >
                {entry.is_dir ? (
                  <Folder size={14} className="shrink-0 text-muted-foreground" />
                ) : (
                  <FileText size={14} className="shrink-0 text-muted-foreground" />
                )}
                <span className="nf-browser-name">{entry.name}</span>
                {entry.is_project && <span className="nf-browser-tag">RELION</span>}
                {entry.is_star && !entry.is_dir && (
                  <span className="nf-browser-tag">STAR</span>
                )}
                <span className="nf-browser-size">{sizeLabel(entry.size)}</span>
              </button>
            ))
          ) : (
            <p className="nf-browser-empty">{tx('此目录为空')}</p>
          )}
          {listing?.truncated && (
            <p className="nf-browser-empty">{tx('条目过多，仅显示前一部分')}</p>
          )}
        </div>

        <div className="nf-browser-footer">
          <span className="nf-browser-target" title={selected?.path || listing?.path}>
            {selected ? selected.name : listing?.path || ''}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={!listing}
            onClick={() => {
              if (!listing) return;
              onPick(toStored(listing.path));
              onOpenChange(false);
            }}
          >
            <FolderOpen />
            {tx('选择当前文件夹')}
          </Button>
          <Button size="sm" disabled={!listing} onClick={choose}>
            <Check />
            {tx('选择')}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
