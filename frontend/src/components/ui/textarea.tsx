import * as React from "react";

import { cn } from "@/lib/utils";

/** Multi-line input on daisyUI's `textarea`. */
function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn("textarea textarea-sm w-full text-xs", className)}
      {...props}
    />
  );
}

export { Textarea };
