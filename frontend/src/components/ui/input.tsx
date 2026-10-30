// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import * as React from "react";

import { cn } from "@/lib/utils";

/** Text input on daisyUI's `input`, which supplies the border, focus and size. */
function Input({ className, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      data-slot="input"
      className={cn("input input-sm w-full text-xs", className)}
      {...props}
    />
  );
}

export { Input };
