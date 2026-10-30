import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Check, X } from "lucide-react";

import { cn } from "@/lib/utils";

export interface Notification {
  id: number;
  text: string;
  error: boolean;
}

/**
 * Transient messages in one place, instead of a single line in the header.
 *
 * The header line never cleared, so the last message stayed on screen forever
 * and there was no way to dismiss it. These stack, dismiss themselves (errors
 * stay longer, since they usually need reading), and can be closed.
 */
export function useNotifications() {
  const [items, setItems] = useState<Notification[]>([]);
  const counter = useRef(0);

  const dismiss = useCallback((id: number) => {
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const notify = useCallback((text: string, error = false) => {
    counter.current += 1;
    const id = counter.current;
    // Keep the newest few only; a burst of errors would otherwise cover the
    // whole screen.
    setItems((current) => [...current.slice(-3), { id, text, error }]);
  }, []);

  return { items, notify, dismiss };
}

interface NotificationStackProps {
  items: Notification[];
  onDismiss: (id: number) => void;
}

export function NotificationStack({
  items,
  onDismiss,
}: NotificationStackProps) {
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-4 z-50 flex flex-col items-center gap-2 px-4">
      {items.map((item) => (
        <Toast key={item.id} item={item} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function Toast({
  item,
  onDismiss,
}: {
  item: Notification;
  onDismiss: (id: number) => void;
}) {
  const [leaving, setLeaving] = useState(false);

  useEffect(() => {
    // Errors stay long enough to read; confirmations get out of the way fast.
    const lifetime = item.error ? 8000 : 3000;
    const show = window.setTimeout(() => {
      setLeaving(true);
      window.setTimeout(() => onDismiss(item.id), 180);
    }, lifetime);
    return () => window.clearTimeout(show);
  }, [item.id, item.error, onDismiss]);

  return (
    <div
      role="status"
      aria-live={item.error ? "assertive" : "polite"}
      className={cn(
        "pointer-events-auto flex max-w-xl items-start gap-2 rounded-box border px-3 py-2 transition-all duration-150",
        "bg-base-100 text-base-content shadow-[0_8px_24px_-8px_rgb(0_0_0/0.28)]",
        item.error
          ? "border-[color-mix(in_srgb,var(--st-failed-text)_40%,var(--rule))]"
          : "border-rule",
        leaving ? "translate-y-1 opacity-0" : "translate-y-0 opacity-100",
      )}
    >
      {item.error ? (
        <AlertCircle className="mt-0.5 size-4 shrink-0 text-st-failed" />
      ) : (
        <Check className="mt-0.5 size-4 shrink-0 text-st-succeeded" />
      )}
      <span className="min-w-0 flex-1 t-ui break-words">{item.text}</span>
      <button
        type="button"
        onClick={() => onDismiss(item.id)}
        className="-mr-1 shrink-0 rounded-[3px] p-0.5 text-ink-faint hover:bg-base-200 hover:text-base-content"
        aria-label="Dismiss"
      >
        <X className="size-3.5" />
      </button>
    </div>
  );
}
