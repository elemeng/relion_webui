import { cn } from "@/lib/utils";

/**
 * Boolean switch, on daisyUI's `toggle`.
 *
 * Native `input[type=checkbox]` underneath, so keyboard and screen readers work
 * without extra wiring - the daisyUI class does the drawing.
 */
function Switch({
  className,
  checked,
  onCheckedChange,
  disabled,
  id,
}: {
  className?: string;
  checked?: boolean;
  onCheckedChange?: (checked: boolean) => void;
  disabled?: boolean;
  id?: string;
}) {
  return (
    <input
      id={id}
      data-slot="switch"
      type="checkbox"
      role="switch"
      className={cn("toggle toggle-sm", className)}
      checked={checked}
      disabled={disabled}
      onChange={(event) => onCheckedChange?.(event.target.checked)}
    />
  );
}

export { Switch };
