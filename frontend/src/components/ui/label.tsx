// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Field label.
 *
 * daisyUI has no label component; its form guidance is a plain `<label>` with
 * `fieldset`/`legend` or the label text styled by the input wrapper. Keeping a
 * thin wrapper means the call sites do not change when the guidance does.
 */
function Label({ className, ...props }: React.ComponentProps<"label">) {
  return (
    <label
      data-slot="label"
      className={cn("t-label text-base-content/85", className)}
      {...props}
    />
  );
}

export { Label };
