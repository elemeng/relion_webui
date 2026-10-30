import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Hover/focus tooltip, on daisyUI's `tooltip`.
 *
 * daisyUI's tooltip is a `data-tip` attribute on the element itself, drawn in
 * CSS - no portal, no positioning library, no mount. That is the right shape
 * here: the only tooltips are option help on a label, which is always a small
 * element in normal flow.
 *
 * `TooltipProvider` is a no-op kept so the call sites do not change.
 */
function TooltipProvider({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}

function Tooltip({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}

/**
 * The trigger carries the tip. `asChild` is accepted for compatibility; the
 * child must be a single element that forwards className and data attributes.
 */
function TooltipTrigger({
  children,
  asChild,
}: {
  children: React.ReactNode;
  asChild?: boolean;
}) {
  void asChild;
  return <>{children}</>;
}

/**
 * The tip text is rendered into the trigger's `data-tip` by `Tip`. Kept as a
 * component so the API reads the same as before.
 */
function TooltipContent({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <span className={cn("hidden", className)}>{children}</span>;
}

/**
 * A tooltip around an element. daisyUI needs the text on the same element that
 * shows the tip, so this is the shape the call sites use.
 */
function Tip({
  text,
  className,
  children,
}: {
  text: string;
  className?: string;
  children: React.ReactElement<{ className?: string }>;
}) {
  return React.cloneElement(children, {
    "data-tip": text,
    className: cn(
      "tooltip tooltip-bottom",
      className,
      children.props.className,
    ),
  } as { className?: string });
}

export { Tip, Tooltip, TooltipContent, TooltipProvider, TooltipTrigger };
