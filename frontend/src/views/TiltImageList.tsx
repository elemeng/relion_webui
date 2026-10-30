import { useMemo, useState } from "react";

import { PathInput } from "@/components/PathInput";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { TiltImage } from "@/lib/types";

interface TiltImageListProps {
  images: TiltImage[];
  currentIndex: number;
  onSelectIndex: (index: number) => void;
  onToggleSelected: (index: number) => void;
  onSelectAll: () => void;
  onSelectNone: () => void;
  onInvert: () => void;
  onSave: (outputDirectory: string) => Promise<void>;
  starFileName: string | null;
  defaultOutputDirectory: string;
}

/** Tilt image list with per-image selection and the save action. */
export function TiltImageList({
  images,
  currentIndex,
  onSelectIndex,
  onToggleSelected,
  onSelectAll,
  onSelectNone,
  onInvert,
  onSave,
  starFileName,
  defaultOutputDirectory,
}: TiltImageListProps) {
  const [filter, setFilter] = useState("");
  const [output, setOutput] = useState(defaultOutputDirectory);
  const [busy, setBusy] = useState(false);

  const kept = images.filter((image) => image.selected).length;

  const visible = useMemo(() => {
    const query = filter.trim().toLowerCase();
    if (!query) return images;
    return images.filter((image) =>
      (image.name ?? "").toLowerCase().includes(query),
    );
  }, [images, filter]);

  if (!starFileName) {
    return (
      <p className="p-4 t-ui text-ink-muted">
        Load a STAR file to see its tilt images.
      </p>
    );
  }

  return (
    <div className="flex h-full flex-col gap-2.5 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <h2 className="t-section text-ink-faint">
          Tilt images
          <Badge tone="muted" className="ml-2">
            {kept}/{images.length}
          </Badge>
        </h2>
      </div>

      <div className="flex flex-wrap gap-1.5">
        <Button variant="outline" size="sm" onClick={onSelectAll}>
          All
        </Button>
        <Button variant="outline" size="sm" onClick={onSelectNone}>
          None
        </Button>
        <Button variant="outline" size="sm" onClick={onInvert}>
          Invert
        </Button>
      </div>

      <Input
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
        placeholder="filter…"
        spellCheck={false}
        className="h-8 text-xs"
      />

      <ul className="min-h-0 flex-1 overflow-auto rounded-[4px] border border-rule">
        {visible.map((image) => {
          const isCurrent = image.index === currentIndex;
          return (
            <li
              key={image.index}
              className={cn(
                "flex items-center gap-2 border-b border-rule-soft px-2 py-1.5 t-label last:border-b-0",
                isCurrent
                  ? "bg-[color-mix(in_srgb,var(--signal)_10%,transparent)]"
                  : "hover:bg-base-200",
              )}
            >
              <input
                type="checkbox"
                checked={image.selected}
                onChange={() => onToggleSelected(image.index)}
                className="size-3.5 shrink-0 cursor-pointer accent-[var(--signal)]"
                aria-label={`Keep ${image.name ?? image.index}`}
              />
              <button
                type="button"
                onClick={() => onSelectIndex(image.index)}
                className="min-w-0 flex-1 truncate text-left"
                title={image.filename ?? image.name ?? ""}
              >
                <span
                  className={cn(
                    !image.selected && "text-ink-faint line-through",
                  )}
                >
                  {image.name ?? `image ${image.index}`}
                </span>
              </button>
              <span className="shrink-0 font-mono t-meta tabular-nums text-ink-faint">
                {image.tilt_angle != null
                  ? `${image.tilt_angle.toFixed(1)}°`
                  : "—"}
              </span>
            </li>
          );
        })}
        {visible.length === 0 && (
          <li className="p-3 t-label text-ink-faint">no matches</li>
        )}
      </ul>

      <div className="flex gap-2">
        <PathInput
          value={output}
          onChange={setOutput}
          placeholder="output directory"
          mode="directory"
          pickerTitle="Choose an output directory"
          className="min-w-0 flex-1 [&_input]:h-8"
        />
        <Button
          size="sm"
          className="shrink-0"
          disabled={busy || !output.trim()}
          onClick={async () => {
            setBusy(true);
            try {
              await onSave(output.trim());
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "Saving…" : "Save STAR"}
        </Button>
      </div>
    </div>
  );
}
