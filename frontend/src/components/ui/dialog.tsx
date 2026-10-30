// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Modal, on daisyUI's `modal` with a native `<dialog>`.
 *
 * The native element gives focus trapping, Escape-to-close, `::backdrop` and
 * the top layer for free - all of which the Radix version implemented by hand.
 * daisyUI styles `dialog.modal` directly, so the visual language matches its
 * other components.
 *
 * Rendering only when `open` is true matches the previous API, where mounting
 * was the same thing as showing.
 */
function Dialog({
  open,
  onOpenChange,
  children,
}: {
  open: boolean;
  onOpenChange?: (open: boolean) => void;
  children: React.ReactNode;
}) {
  const ref = React.useRef<HTMLDialogElement>(null);

  React.useEffect(() => {
    const node = ref.current;
    if (!node) return;
    if (open && !node.open) node.showModal();
    if (!open && node.open) node.close();
  }, [open]);

  // Escape closes the native dialog without telling React, so the state has to
  // be brought back into step or reopening stops working.
  React.useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const onClose = () => onOpenChange?.(false);
    const onCancel = (event: Event) => {
      event.preventDefault();
      node.close();
    };
    node.addEventListener("close", onClose);
    node.addEventListener("cancel", onCancel);
    return () => {
      node.removeEventListener("close", onClose);
      node.removeEventListener("cancel", onCancel);
    };
  }, [onOpenChange]);

  return (
    <dialog ref={ref} className={cn("modal", open && "modal-open")}>
      {open && children}
    </dialog>
  );
}

function DialogContent({
  className,
  children,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-content"
      className={cn(
        "modal-box flex max-h-[85svh] max-w-2xl flex-col gap-3 p-0",
        "border-base-300 bg-base-100 rounded-[4px] border",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}

function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-header"
      className={cn(
        "flex flex-col gap-1 border-b border-base-300 px-4 py-3",
        className,
      )}
      {...props}
    />
  );
}

function DialogTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return (
    <h2
      data-slot="dialog-title"
      className={cn("text-base leading-tight font-semibold", className)}
      {...props}
    />
  );
}

function DialogDescription({ className, ...props }: React.ComponentProps<"p">) {
  return (
    <p
      data-slot="dialog-description"
      className={cn("text-base-content/70 text-xs", className)}
      {...props}
    />
  );
}

function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn(
        "flex items-center gap-2 border-t border-base-300 px-4 py-2.5",
        className,
      )}
      {...props}
    />
  );
}

export {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
};
