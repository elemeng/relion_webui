// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { cn } from "@/lib/utils";

/**
 * A segmented control: two or three mutually exclusive views of the same data.
 *
 * Three copies of this existed with three different looks - one dark-filled,
 * two bordered, all with slightly different paddings - which is exactly the
 * kind of drift that makes an interface feel assembled rather than designed.
 * daisyUI's `join` gives the shared-border behaviour, so this is mostly a
 * naming layer over it.
 */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  className,
  ariaLabel,
}: {
  value: T;
  options: readonly (readonly [T, string])[];
  onChange: (value: T) => void;
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <div role="group" aria-label={ariaLabel} className={cn("join", className)}>
      {options.map(([option, label], index) => {
        const active = option === value;
        return (
          <button
            key={option}
            type="button"
            onClick={() => onChange(option)}
            aria-pressed={active}
            className={cn(
              "btn join-item btn-xs h-6 min-h-0 border-rule px-2 t-label font-medium",
              "shadow-none",
              index === 0 && "rounded-l-[4px]",
              index === options.length - 1 && "rounded-r-[4px]",
              active
                ? "btn-active border-rule bg-base-100 text-base-content"
                : "border-rule bg-base-200 text-ink-muted hover:text-base-content",
            )}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
