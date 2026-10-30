// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { useState } from "react";
import { FolderSearch } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PathPicker, type BrowseFilter } from "@/components/PathPicker";
import { cn } from "@/lib/utils";

interface PathInputProps {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  /** What the picker should show; also decides the picker's confirm label. */
  mode: BrowseFilter;
  /** Start browsing here; defaults to the current value. */
  pickerTitle?: string;
  className?: string;
  disabled?: boolean;
}

/**
 * Text field plus a browse button.
 *
 * Typing is still allowed — a path may be pasted from a job log or a
 * colleague — but the button opens the agent's filesystem browser, which is
 * the only way to see what exists on the remote machine.
 */
export function PathInput({
  id,
  value,
  onChange,
  placeholder,
  mode,
  pickerTitle,
  className,
  disabled,
}: PathInputProps) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <div className={cn("flex min-w-0 flex-1 gap-2", className)}>
        <Input
          id={id}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          spellCheck={false}
          disabled={disabled}
          className="min-w-0 flex-1 font-mono"
        />
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="shrink-0"
          disabled={disabled}
          onClick={() => setOpen(true)}
          title="Browse the agent's filesystem"
        >
          <FolderSearch className="size-4" />
        </Button>
      </div>

      <PathPicker
        open={open}
        onOpenChange={setOpen}
        onPick={onChange}
        mode={mode}
        title={pickerTitle}
        initialPath={value || undefined}
      />
    </>
  );
}
