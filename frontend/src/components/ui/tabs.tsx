import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Tabs, on daisyUI's `tabs` / `tab` classes.
 *
 * A small context rather than a hidden state machine: `Tabs` owns the value,
 * `TabsTrigger` reports clicks, `TabsContent` renders only when selected. The
 * daisyUI class supplies the underline, hover and active colour, so there is
 * nothing visual to reimplement - and no `!important` overrides, which is what
 * the previous version needed to undo daisyUI's own styling.
 */
const TabsContext = React.createContext<{
  value: string;
  setValue: (value: string) => void;
} | null>(null);

function Tabs({
  value,
  onValueChange,
  className,
  children,
}: {
  value: string;
  onValueChange: (value: string) => void;
  className?: string;
  children: React.ReactNode;
}) {
  const context = React.useMemo(
    () => ({ value, setValue: onValueChange }),
    [value, onValueChange],
  );
  return (
    <TabsContext.Provider value={context}>
      <div data-slot="tabs" className={cn("flex min-h-0 flex-col", className)}>
        {children}
      </div>
    </TabsContext.Provider>
  );
}

function TabsList({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      role="tablist"
      className={cn("tabs tabs-border w-full justify-start gap-0", className)}
    >
      {children}
    </div>
  );
}

function TabsTrigger({
  value,
  className,
  children,
}: {
  value: string;
  className?: string;
  children: React.ReactNode;
}) {
  const context = React.useContext(TabsContext);
  const active = context?.value === value;
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={() => context?.setValue(value)}
      className={cn(
        "tab h-auto flex-none gap-1.5 px-3 pt-2 pb-1.5 t-ui whitespace-nowrap",
        active
          ? "tab-active font-medium"
          : "text-ink-muted hover:text-base-content",
        className,
      )}
    >
      {children}
    </button>
  );
}

function TabsContent({
  value,
  className,
  children,
}: {
  value: string;
  className?: string;
  children: React.ReactNode;
}) {
  const context = React.useContext(TabsContext);
  if (context?.value !== value) return null;
  return (
    <div role="tabpanel" className={cn("min-h-0", className)}>
      {children}
    </div>
  );
}

export { Tabs, TabsContent, TabsList, TabsTrigger };
