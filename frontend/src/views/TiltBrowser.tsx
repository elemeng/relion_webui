// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import { FolderOpen, ImageOff } from "lucide-react";

import { TiltImageList } from "@/views/TiltImageList";
import { ImageViewer } from "@/views/ImageViewer";
import { PathInput } from "@/components/PathInput";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type {
  DatasetSummary,
  TiltSeriesDetail,
  TiltSeriesInfo,
} from "@/lib/types";

interface TiltBrowserProps {
  dataset: DatasetSummary | null;
  detail: TiltSeriesDetail | null;
  selectedName: string | null;
  imageIndex: number;
  defaultOutputDirectory: string;
  onLoadStar: (path: string) => Promise<void>;
  onSelectSeries: (name: string) => Promise<void>;
  onImageIndexChange: (index: number) => void;
  onToggleImage: (index: number) => void;
  onSelectAll: () => void;
  onSelectNone: () => void;
  onInvert: () => void;
  onSaveSelection: (outputDirectory: string) => Promise<void>;
}

/**
 * Tilt-series browser: the list, the image, and the per-image selection.
 *
 * This is a mode of the canvas rather than a column beside it. Three fixed
 * columns squeezed the image below ~1400 px, and the pipeline - the thing you
 * are usually looking at - lost its centre.
 */
export function TiltBrowser({
  dataset,
  detail,
  selectedName,
  imageIndex,
  defaultOutputDirectory,
  onLoadStar,
  onSelectSeries,
  onImageIndexChange,
  onToggleImage,
  onSelectAll,
  onSelectNone,
  onInvert,
  onSaveSelection,
}: TiltBrowserProps) {
  const [path, setPath] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!dataset) {
    return (
      <div className="grid h-full place-items-center p-8">
        <div className="w-full max-w-md">
          <div className="mb-4 text-center">
            <ImageOff className="mx-auto mb-2 size-6 text-ink-faint" />
            <p className="t-ui font-medium">No tilt-series set loaded</p>
            <p className="mt-1 t-body leading-relaxed text-ink-muted">
              Open the STAR file that lists the tilt-series, or the one a job
              wrote.
            </p>
          </div>
          <form
            className="flex gap-2"
            onSubmit={async (event) => {
              event.preventDefault();
              if (!path.trim()) return;
              setLoading(true);
              setError(null);
              try {
                await onLoadStar(path.trim());
              } catch (caught) {
                setError(
                  caught instanceof Error
                    ? caught.message
                    : "could not load it",
                );
              } finally {
                setLoading(false);
              }
            }}
          >
            <PathInput
              value={path}
              onChange={setPath}
              placeholder="/path/to/tilt_series.star"
              mode="star"
              pickerTitle="Choose a tilt-series STAR file"
              className="min-w-0 flex-1"
            />
            <Button type="submit" size="sm" disabled={loading || !path.trim()}>
              {loading ? (
                <Spinner size="sm" />
              ) : (
                <FolderOpen className="size-3.5" />
              )}
              Open
            </Button>
          </form>
          {error && (
            <p className="mt-2 t-label text-st-failed" role="alert">
              {error}
            </p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="grid h-full grid-cols-[13.75rem_minmax(0,1fr)_18.75rem] overflow-hidden">
      <aside className="flex min-h-0 flex-col border-r border-rule bg-base-100">
        <div className="flex items-center gap-1.5 border-b border-rule px-3 py-2">
          <span className="t-section text-ink-faint">Tilt-series</span>
          <span className="font-mono t-meta text-ink-faint">
            {dataset.n_tilt_series}
          </span>
        </div>
        <ul className="min-h-0 flex-1 overflow-y-auto p-1.5">
          {dataset.tilt_series.map((info) => (
            <TiltSeriesRow
              key={info.name}
              info={info}
              active={info.name === selectedName}
              onSelect={() => onSelectSeries(info.name)}
            />
          ))}
        </ul>
      </aside>

      <section className="flex min-h-0 min-w-0 flex-col bg-base-100">
        <div className="flex items-center gap-2 border-b border-rule px-3 py-2">
          <span className="truncate t-ui font-medium">
            {selectedName ?? "Select a tilt-series"}
          </span>
          {detail && (
            <span className="font-mono t-meta text-ink-faint">
              {detail.images.length} tilts
              {detail.pixel_size
                ? ` · ${detail.pixel_size.toFixed(3)} Å/px`
                : ""}
            </span>
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-hidden">
          <ImageViewer
            starFile={dataset.star_file}
            detail={detail}
            name={selectedName}
            imageIndex={imageIndex}
            onImageIndexChange={onImageIndexChange}
          />
        </div>
      </section>

      <aside className="min-h-0 overflow-hidden border-l border-rule bg-base-100">
        <TiltImageList
          images={detail?.images ?? []}
          currentIndex={imageIndex}
          onSelectIndex={onImageIndexChange}
          onToggleSelected={onToggleImage}
          onSelectAll={onSelectAll}
          onSelectNone={onSelectNone}
          onInvert={onInvert}
          onSave={onSaveSelection}
          starFileName={selectedName}
          defaultOutputDirectory={defaultOutputDirectory}
        />
      </aside>
    </div>
  );
}

function TiltSeriesRow({
  info,
  active,
  onSelect,
}: {
  info: TiltSeriesInfo;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        className={cn(
          "flex w-full items-center gap-1.5 rounded-selector px-2 py-1.5 text-left t-label transition-colors",
          active
            ? "bg-[color-mix(in_srgb,var(--signal)_12%,transparent)] text-base-content"
            : "text-base-content hover:bg-base-200",
        )}
        title={info.star_file ?? info.name}
      >
        <span className="min-w-0 flex-1 truncate">{info.name}</span>
        {info.tomogram_file && (
          <span className="shrink-0 rounded-[0.125rem] bg-[var(--st-succeeded-fill)] px-1 t-meta text-[var(--st-succeeded-text)]">
            tomo
          </span>
        )}
        {info.denoised_tomogram_file && (
          <span className="shrink-0 rounded-[0.125rem] bg-[var(--st-running-fill)] px-1 t-meta text-[var(--st-running-text)]">
            den
          </span>
        )}
      </button>
    </li>
  );
}
