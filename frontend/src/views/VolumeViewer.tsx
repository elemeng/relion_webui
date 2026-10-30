// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import { Crosshair, RotateCcw, ZoomIn } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectItem } from "@/components/ui/select";
import { cn } from "@/lib/utils";

export interface PickedPoint {
  /** Fractional position in the volume, 0..1 on each axis. */
  x: number;
  y: number;
  z: number;
  /** Ray depth as a fraction, for reporting how far in the click landed. */
  depth: number;
}

interface VolumeViewerProps {
  path: string | null;
  /** Called when the user clicks the volume to place a point. */
  onPick?: (point: PickedPoint) => void;
  picks?: PickedPoint[];
  className?: string;
}

type ProjectionMode = "mip" | "mean";

/**
 * A rotatable maximum-intensity view of a tomogram.
 *
 * The desktop GUI's 3D view is napari, which renders the volume client-side.
 * A browser cannot hold a multi-gigabyte tomogram, so this asks the server for
 * a projection from the current angle instead: dragging changes yaw/pitch and
 * fetches a new frame. That keeps memory bounded and works over a LAN, at the
 * cost of a frame round-trip while dragging — so frames are fetched at a low
 * resolution during the drag and re-rendered at full size when it stops.
 *
 * Picking is a click on the rendered frame. The server returns a projection
 * rather than a depth map, so the depth is taken as the middle of the volume;
 * `onPick` therefore reports a point on the mid-plane, which is enough for
 * placing a marker and matches how the viewer shows it.
 */
export function VolumeViewer({
  path,
  onPick,
  picks = [],
  className,
}: VolumeViewerProps) {
  const [yaw, setYaw] = useState(-30);
  const [pitch, setPitch] = useState(20);
  const [mode, setMode] = useState<ProjectionMode>("mip");
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [size, setSize] = useState(512);
  const dragRef = useRef<{
    x: number;
    y: number;
    yaw: number;
    pitch: number;
  } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // A smaller render while dragging keeps the frame round-trip tolerable.
  const renderSize = dragging ? 256 : size;

  const url = useMemo(() => {
    if (!path) return null;
    return (
      `/api/render/volume?path=${encodeURIComponent(path)}` +
      `&yaw=${yaw.toFixed(2)}&pitch=${pitch.toFixed(2)}` +
      `&mode=${mode}&width=${renderSize}&height=${renderSize}`
    );
  }, [path, yaw, pitch, mode, renderSize]);

  useEffect(() => {
    if (!path) return;
    setLoading(true);
    setError(null);
    // Preloading gives a definite "finished" signal, which a plain <img> src
    // swap does not: without it the spinner would never clear.
    const image = new Image();
    image.onload = () => setLoading(false);
    image.onerror = () => {
      setLoading(false);
      setError("could not render this volume");
    };
    image.src = url ?? "";
    return () => {
      image.onload = null;
      image.onerror = null;
    };
  }, [url, path]);

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!path) return;
      dragRef.current = {
        x: event.clientX,
        y: event.clientY,
        yaw,
        pitch,
      };
      setDragging(true);
      event.currentTarget.setPointerCapture(event.pointerId);
    },
    [path, yaw, pitch],
  );

  const onPointerMove = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      if (!drag) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      setYaw(clamp(drag.yaw + dx * 0.5, -180, 180));
      setPitch(clamp(drag.pitch - dy * 0.5, -90, 90));
    },
    [],
  );

  const endDrag = useCallback(() => {
    dragRef.current = null;
    setDragging(false);
  }, []);

  const onPointerUp = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      endDrag();
      if (!drag || !path || !onPick) return;
      // A drag that moved more than a few pixels was a rotation, not a click.
      if (
        Math.abs(event.clientX - drag.x) > 4 ||
        Math.abs(event.clientY - drag.y) > 4
      ) {
        return;
      }
      const box = event.currentTarget.getBoundingClientRect();
      const fx = (event.clientX - box.left) / box.width;
      const fy = (event.clientY - box.top) / box.height;
      onPick({
        x: clamp(fx, 0, 1),
        y: clamp(fy, 0, 1),
        z: 0.5,
        depth: 0.5,
      });
    },
    [endDrag, onPick, path],
  );

  if (!path) {
    return (
      <p className={cn("text-base-content/60 text-sm", className)}>
        Load a tomogram to view it in 3D.
      </p>
    );
  }

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={mode}
          onValueChange={(value) => setMode(value as ProjectionMode)}
          className="w-32"
        >
          <SelectItem value="mip">Max intensity</SelectItem>
          <SelectItem value="mean">Mean</SelectItem>
        </Select>

        <Select
          value={String(size)}
          onValueChange={(value) => setSize(Number(value))}
          className="w-24"
        >
          {[384, 512, 768].map((value) => (
            <SelectItem key={value} value={String(value)}>
              {value}px
            </SelectItem>
          ))}
        </Select>

        <Button
          variant="outline"
          size="sm"
          className="h-7 text-xs"
          onClick={() => {
            setYaw(-30);
            setPitch(20);
          }}
        >
          <RotateCcw className="size-3" />
          reset
        </Button>

        <Badge tone="muted" className="font-mono t-meta">
          yaw {yaw.toFixed(0)}° · pitch {pitch.toFixed(0)}°
        </Badge>

        {onPick && (
          <Badge tone="muted" className="gap-1 t-meta">
            <Crosshair className="size-2.5" />
            click to place a point
          </Badge>
        )}
      </div>

      <div
        ref={containerRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={endDrag}
        className={cn(
          "relative aspect-square w-full overflow-hidden rounded-[4px] border border-rule bg-base-200",
          path ? "cursor-grab active:cursor-grabbing" : "",
        )}
      >
        {url && (
          <img
            src={url}
            alt="tomogram projection"
            className="pointer-events-none h-full w-full object-contain select-none"
            draggable={false}
          />
        )}

        {picks.map((pick, index) => (
          <span
            key={`${pick.x}-${pick.y}-${index}`}
            className="pointer-events-none absolute size-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-red-500 ring-2 ring-white/70"
            style={{ left: `${pick.x * 100}%`, top: `${pick.y * 100}%` }}
            title={`(${pick.x.toFixed(2)}, ${pick.y.toFixed(2)}, ${pick.z.toFixed(2)})`}
          />
        ))}

        {loading && (
          <span className="bg-base-200/60 absolute inset-0 flex items-center justify-center">
            <Spinner size="md" className="text-base-content/60" />
          </span>
        )}

        {error && (
          <span className="text-error bg-base-200/80 absolute inset-0 flex items-center justify-center p-4 text-center text-xs">
            {error}
          </span>
        )}
      </div>

      <p className="flex items-center gap-1 t-label text-ink-muted">
        <ZoomIn className="size-3" />
        Drag to rotate. The projection is rendered server-side, so a multi-GB
        tomogram is never downloaded.
      </p>

      {picks.length > 0 && (
        <div className="t-label text-ink-muted">
          <Label>{picks.length} point(s)</Label>
          <ul className="mt-1 max-h-24 overflow-auto font-mono t-meta">
            {picks.map((pick, index) => (
              <li key={index}>
                {index + 1}: x={pick.x.toFixed(3)} y={pick.y.toFixed(3)} z=
                {pick.z.toFixed(3)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}
