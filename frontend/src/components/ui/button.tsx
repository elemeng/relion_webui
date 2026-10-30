import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * Button.
 *
 * daisyUI's `btn` supplies the whole interaction model - focus ring, disabled
 * state, active press, spacing, the colour roles - so this only maps our
 * variant names onto its modifiers. Nothing about a button is re-implemented.
 */
const buttonVariants = cva("btn", {
  variants: {
    variant: {
      default: "btn-primary",
      destructive: "btn-error",
      outline: "btn-outline border-base-300",
      secondary: "btn-soft",
      ghost: "btn-ghost border-0 shadow-none",
      link: "btn-link",
    },
    size: {
      default: "btn-sm",
      sm: "btn-xs",
      lg: "btn-md",
      icon: "btn-sm btn-square",
    },
  },
  defaultVariants: { variant: "default", size: "default" },
});

function Button({
  className,
  variant,
  size,
  ...props
}: React.ComponentProps<"button"> & VariantProps<typeof buttonVariants>) {
  return (
    <button
      data-slot="button"
      className={cn(
        buttonVariants({ variant, size }),
        "h-auto min-h-0 font-medium [&_svg]:size-3.5",
        className,
      )}
      {...props}
    />
  );
}

export { Button, buttonVariants };
