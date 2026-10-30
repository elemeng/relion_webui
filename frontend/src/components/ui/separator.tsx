// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { cn } from "@/lib/utils";

/** Hairline rule. daisyUI's `divider` is the horizontal form; this covers both. */
function Separator({
  className,
  orientation = "horizontal",
}: {
  className?: string;
  orientation?: "horizontal" | "vertical";
}) {
  return (
    <div
      data-slot="separator"
      role="separator"
      className={cn(
        "bg-base-300 shrink-0",
        orientation === "horizontal" ? "h-px w-full" : "h-full w-px",
        className,
      )}
    />
  );
}

export { Separator };
