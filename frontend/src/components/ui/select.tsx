import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Dropdown, on a native `<select>` styled by daisyUI's `select`.
 *
 * A listbox built from divs has to re-implement keyboard navigation, type-ahead,
 * screen-reader semantics and viewport collision - all of which the platform
 * already provides and gets right. daisyUI styles the native control directly,
 * so the popup is the OS one and the closed state matches the rest of the form.
 *
 * The old Radix-based API (`Select`/`SelectTrigger`/`SelectItem`) was a compound
 * tree; these take the simpler shape the call sites actually used.
 */
function Select({
  value,
  onValueChange,
  disabled,
  id,
  className,
  children,
}: {
  value: string;
  onValueChange: (value: string) => void;
  disabled?: boolean;
  id?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <select
      id={id}
      data-slot="select"
      className={cn("select select-sm w-full text-xs", className)}
      value={value}
      disabled={disabled}
      onChange={(event) => onValueChange(event.target.value)}
    >
      {children}
    </select>
  );
}

function SelectItem({
  value,
  children,
  className,
}: {
  value: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <option value={value} className={className}>
      {children}
    </option>
  );
}

export { Select, SelectItem };
