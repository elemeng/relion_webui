// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Spinner, on daisyUI's `loading`.
 *
 * Every "working…" indicator in the app was a `Loader2` with `animate-spin`,
 * written out 21 times in nine slightly different size/colour combinations.
 * daisyUI already owns this: `loading loading-spinner` is the documented
 * component, it animates the same way, and it inherits the text colour, so the
 * caller only picks a size.
 *
 * `className` still applies, which is how a spinner gets its colour:
 * `text-base-content/60`, `text-ink-faint`.
 *
 * A rotating icon is still the right choice in one place - a refresh button
 * whose own glyph spins to show a request is in flight - because there the
 * spinner *is* the button's icon rather than a separate indicator.
 */
type SpinnerSize = "xs" | "sm" | "md" | "lg";

const SIZES: Record<SpinnerSize, string> = {
  xs: "loading-xs",
  sm: "loading-sm",
  md: "loading-md",
  lg: "loading-lg",
};

function Spinner({
  className,
  size = "sm",
  label = "loading",
  ...props
}: React.ComponentProps<"span"> & { size?: SpinnerSize; label?: string }) {
  return (
    <span
      data-slot="spinner"
      role="status"
      aria-label={label}
      className={cn("loading loading-spinner", SIZES[size], className)}
      {...props}
    />
  );
}

export { Spinner };
