// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import {
  ChevronRight,
  Copy,
  Eye,
  FileText,
  MoreHorizontal,
  ScrollText,
  Square,
  Trash2,
  Wrench,
} from "lucide-react";

import { cn } from "@/lib/utils";

export type NodeAction =
  | "open"
  | "details"
  | "parameters"
  | "outputs"
  | "log"
  | "use-as-input"
  | "stop"
  | "actions"
  | "remove-draft";

const LABELS: Record<NodeAction, string> = {
  open: "Open",
  details: "Overview",
  parameters: "Inputs & parameters",
  outputs: "Outputs",
  log: "Log",
  "use-as-input": "Use as input…",
  stop: "Stop",
  actions: "Job actions…",
  "remove-draft": "Remove draft",
};

/** Grouped, because six equal rows read as one undifferentiated list. */
const GROUPS: NodeAction[][] = [
  ["open", "details", "parameters", "outputs", "log"],
  ["use-as-input"],
  ["stop", "actions"],
];

const DESTRUCTIVE = new Set<NodeAction>(["stop", "remove-draft"]);

/**
 * The per-node menu.
 *
 * Puts a job's actions on the card so you do not have to open it to
 * act on it; ComfyUI puts them in a small header menu so the card stays compact.
 * Both apply here: the menu is the card's own affordance, and each entry jumps
 * straight to the tab or action instead of a summary dialog.
 *
 * Rendered in a portal at the viewport rather than inside the card. A card is
 * `overflow-hidden` (so its status rail and rounded corners clip cleanly), which
 * would otherwise cut the menu off at the card's own edge - measured at 544px
 * against a card bottom of 513px, i.e. three of six entries invisible.
 */
export function NodeMenu({
  actions,
  onAction,
  className,
}: {
  actions: NodeAction[];
  onAction: (action: NodeAction) => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{
    left: number;
    top: number;
  } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const place = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const width = menuRef.current?.offsetWidth ?? 188;
    const height = menuRef.current?.offsetHeight ?? 240;
    // Right-aligned to the trigger, then kept inside the viewport.
    let left = rect.right - width;
    left = Math.max(6, Math.min(left, window.innerWidth - width - 6));
    let top = rect.bottom + 3;
    if (top + height > window.innerHeight - 6) {
      // Not enough room below: flip above the trigger.
      top = Math.max(6, rect.top - height - 3);
    }
    setPosition({ left, top });
  }, []);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      const target = event.target as Node;
      if (menuRef.current?.contains(target)) return;
      if (rootRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        // Capture phase: the canvas also listens for Escape and would clear
        // the selection instead of just closing the menu.
        event.stopPropagation();
        event.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    const reposition = () => place();
    document.addEventListener("pointerdown", close, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      document.removeEventListener("pointerdown", close, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open, place]);

  const entries = GROUPS.map((group) =>
    group.filter((action) => actions.includes(action)),
  ).filter((group) => group.length > 0);

  return (
    <div
      ref={rootRef}
      className={cn("relative", className)}
      // The card itself opens the dialog; the menu must not bubble to it.
      onClick={(event) => event.stopPropagation()}
    >
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label="Job actions"
        className={cn(
          "grid size-5 place-items-center rounded-[3px] text-ink-faint",
          "hover:bg-base-200 hover:text-base-content",
          open && "bg-base-200 text-base-content",
        )}
        title="Actions"
      >
        <MoreHorizontal className="size-3.5" />
      </button>

      {open &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label="Job actions"
            style={{
              left: position?.left ?? -9999,
              top: position?.top ?? -9999,
            }}
            className={cn(
              "fixed z-[60] w-[188px] overflow-hidden rounded-box border border-rule",
              "bg-base-100 py-1 shadow-[0_6px_20px_-6px_rgb(0_0_0/0.28),0_2px_6px_-2px_rgb(0_0_0/0.16)]",
            )}
            onClick={(event) => event.stopPropagation()}
          >
            {entries.map((group, index) => (
              <div
                key={index}
                className={cn(
                  index > 0 && "mt-1 border-t border-rule-soft pt-1",
                )}
              >
                {group.map((action) => (
                  <button
                    key={action}
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setOpen(false);
                      onAction(action);
                    }}
                    className={cn(
                      "flex w-full items-center gap-2 px-2.5 py-1.5 text-left t-ui",
                      "hover:bg-base-200",
                      DESTRUCTIVE.has(action)
                        ? "text-st-failed"
                        : "text-base-content",
                    )}
                  >
                    <Icon
                      action={action}
                      destructive={DESTRUCTIVE.has(action)}
                    />
                    <span className="flex-1 truncate">{LABELS[action]}</span>
                  </button>
                ))}
              </div>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
}

function Icon({
  action,
  destructive,
}: {
  action: NodeAction;
  destructive: boolean;
}) {
  const className = cn(
    "size-3.5 shrink-0",
    destructive ? "text-st-failed" : "text-ink-faint",
  );
  switch (action) {
    case "open":
      return <Eye className={className} />;
    case "details":
      return <ChevronRight className={className} />;
    case "parameters":
      return <Wrench className={className} />;
    case "outputs":
      return <FileText className={className} />;
    case "log":
      return <ScrollText className={className} />;
    case "use-as-input":
      return <Copy className={className} />;
    case "stop":
      return <Square className={className} />;
    case "remove-draft":
      return <Trash2 className={className} />;
    default:
      return <Wrench className={className} />;
  }
}
