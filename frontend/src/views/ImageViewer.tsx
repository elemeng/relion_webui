// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useEffect, useMemo, useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import { Box, ImageOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Select, SelectItem } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { VolumeViewer, type PickedPoint } from "@/views/VolumeViewer";
import type { TiltSeriesDetail } from "@/lib/types";

export type ViewerMode = "slice" | "projection" | "volume";

interface ImageViewerProps {
  starFile: string | null;
  detail: TiltSeriesDetail | null;
  /** Selected tilt-series name. */
  name: string | null;
  imageIndex: number;
  onImageIndexChange: (index: number) => void;
}

/**
 * Tilt image / projection view.
 *
 * Images are rendered server-side as PNG, so the browser never downloads a
 * tomogram; the Z-slice comes from the server's memory map.
 */
export function ImageViewer({
  starFile,
  detail,
  name,
  imageIndex,
  onImageIndexChange,
}: ImageViewerProps) {
  const [mode, setMode] = useState<ViewerMode>("slice");
  const [axis, setAxis] = useState<"z" | "y" | "x">("z");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const [picks, setPicks] = useState<PickedPoint[]>([]);
  const [savingPicks, setSavingPicks] = useState(false);
  const [pickMessage, setPickMessage] = useState<string | null>(null);

  const images = detail?.images ?? [];
  const current = images[imageIndex];

  // A new tilt-series resets the view rather than keeping the old contrast.
  useEffect(() => {
    setMode("slice");
    setError(null);
    // Picks belong to one tomogram, so a new tilt-series starts empty.
    setPicks([]);
    setPickMessage(null);
  }, [name]);

  const url = useMemo(() => {
    // The 3D view fetches its own frames as the user rotates, so this URL is
    // only for the 2D modes.
    if (!detail || mode === "volume") return null;
    if (mode === "projection") {
      if (!detail.tomogram_file) return null;
      return `/api/render/projection?path=${encodeURIComponent(
        detail.tomogram_file,
      )}&axis=${axis}&width=900&height=900&t=${nonce}`;
    }
    if (!starFile || !name) return null;
    return `/api/render/tilt-image?star_file=${encodeURIComponent(
      starFile,
    )}&name=${encodeURIComponent(name)}&index=${imageIndex}&width=900&height=900&t=${nonce}`;
  }, [detail, mode, axis, starFile, name, imageIndex, nonce]);

  useEffect(() => {
    if (url) {
      setLoading(true);
      setError(null);
    }
  }, [url]);

  const canProject = Boolean(detail?.tomogram_file);
  // Captured outside the closure so the narrowing survives into the handler.
  const tomogramFile = detail?.tomogram_file ?? null;
  const meta = [
    current?.tilt_angle != null && `tilt ${current.tilt_angle.toFixed(1)}°`,
    current?.defocus != null && `defocus ${current.defocus.toFixed(0)} Å`,
    detail?.pixel_size && `pixel ${detail.pixel_size.toFixed(2)} Å`,
  ].filter(Boolean) as string[];

  if (!detail) {
    return (
      <div className="flex h-full min-h-80 flex-col items-center justify-center gap-3 rounded-xl border border-dashed">
        <ImageOff className="text-base-content/60/50 size-8" />
        <p className="text-base-content/60 text-sm">
          Open a STAR file and pick a tilt-series.
        </p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={axis}
          onValueChange={(value) => setAxis(value as typeof axis)}
          className="w-[150px]"
        >
          <SelectItem value="z">z · xy plane</SelectItem>
          <SelectItem value="y">y · xz plane</SelectItem>
          <SelectItem value="x">x · yz plane</SelectItem>
        </Select>

        <div className="flex items-center gap-2">
          <Button
            variant={mode === "slice" ? "secondary" : "ghost"}
            size="sm"
            onClick={() => setMode("slice")}
          >
            Slice
          </Button>
          <Button
            variant={mode === "projection" ? "secondary" : "ghost"}
            size="sm"
            disabled={!canProject}
            onClick={() => setMode("projection")}
            title={
              canProject
                ? "Mean projection along z"
                : "No reconstructed tomogram for this tilt-series"
            }
          >
            Projection
          </Button>
          <Button
            variant={mode === "volume" ? "secondary" : "ghost"}
            size="sm"
            disabled={!canProject}
            onClick={() => setMode("volume")}
            title={
              canProject
                ? "Rotatable 3D view of the tomogram"
                : "No reconstructed tomogram for this tilt-series"
            }
          >
            <Box className="size-3.5" />
            3D
          </Button>
        </div>

        <div className="ml-auto flex items-center gap-2">
          {mode === "slice" && images.length > 0 && (
            <>
              <input
                type="range"
                min={0}
                max={images.length - 1}
                value={imageIndex}
                onChange={(event) =>
                  onImageIndexChange(Number(event.target.value))
                }
                className="accent-primary h-1.5 w-40 cursor-pointer"
                aria-label="Tilt image"
              />
              <span className="text-base-content/60 w-14 text-right font-mono text-xs tabular-nums">
                {imageIndex + 1}/{images.length}
              </span>
            </>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => setNonce((value) => value + 1)}
            disabled={!url}
          >
            Refresh
          </Button>
        </div>
      </div>

      {mode === "volume" && tomogramFile ? (
        <div className="flex flex-1 flex-col gap-2">
          <VolumeViewer
            path={tomogramFile}
            picks={picks}
            onPick={(point) => setPicks((current) => [...current, point])}
          />
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              disabled={savingPicks || picks.length === 0}
              onClick={async () => {
                setSavingPicks(true);
                setPickMessage(null);
                try {
                  const response = await api.post<{
                    star_file: string;
                    n_points: number;
                  }>("/api/project/pick-points", {
                    tomogram_path: tomogramFile,
                    output_directory: `${tomogramFile.split("/").slice(0, -1).join("/")}/picks`,
                    points: picks,
                  });
                  setPickMessage(
                    `saved ${response.n_points} point(s) to ${response.star_file}`,
                  );
                } catch (error) {
                  setPickMessage(
                    error instanceof Error
                      ? error.message
                      : "could not save picks",
                  );
                } finally {
                  setSavingPicks(false);
                }
              }}
            >
              Save {picks.length} point(s)
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={picks.length === 0}
              onClick={() => {
                setPicks([]);
                setPickMessage(null);
              }}
            >
              Clear
            </Button>
            {pickMessage && (
              <span className="truncate t-label text-ink-muted">
                {pickMessage}
              </span>
            )}
          </div>
        </div>
      ) : (
        <div className="relative flex flex-1 items-center justify-center overflow-hidden rounded-box border border-rule bg-base-200">
          {error ? (
            <div className="flex flex-col items-center gap-2 p-6 text-center">
              <ImageOff className="size-7 text-ink-faint" />
              <p className="max-w-md t-ui text-st-failed">{error}</p>
            </div>
          ) : url ? (
            <>
              {loading && (
                <Spinner size="lg" className="absolute text-ink-faint" />
              )}
              <img
                key={url}
                src={url}
                alt={
                  mode === "projection"
                    ? "Tomogram projection"
                    : `Tilt image ${imageIndex + 1}`
                }
                // Fill the frame rather than sit at its intrinsic size: a 64px
                // tilt image rendered 1:1 left most of the window empty, which
                // reads as a viewer that failed rather than a small image.
                className={cn(
                  "image-pixelated absolute inset-0 size-full object-contain p-2",
                  loading && "opacity-0",
                )}
                onLoad={() => setLoading(false)}
                onError={() => {
                  setLoading(false);
                  setError(
                    mode === "projection"
                      ? "Could not render the projection."
                      : "Could not render this tilt image — the file may be missing.",
                  );
                }}
              />
            </>
          ) : (
            <p className="text-base-content/60 p-6 text-sm">
              {mode === "projection"
                ? "This tilt-series has no reconstructed tomogram."
                : "No image selected."}
            </p>
          )}
        </div>
      )}

      <p className="text-base-content/60 min-h-4 text-xs">
        {mode === "volume"
          ? "server-rendered 3D projection"
          : mode === "projection"
            ? `projection along ${axis}`
            : meta.join(" · ")}
      </p>
    </div>
  );
}
