/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

import { useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

import { ChevronRight, Info } from 'lucide-react';

import { Collapse } from '../ui/presence';
import { errorText } from '../../lib/i18n';

/**
 * A hover tip next to the label.
 *
 * Long option help (RELION's paragraph per field) does not belong inline -
 * under every field it buries the value. The Info glyph carries it instead:
 * hovering, focusing or clicking the icon opens a small detached panel, and
 * the field keeps its height. Styled with the inspector's own tokens, so it
 * reads as part of the form rather than a browser tooltip.
 */
function LabelTip({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  // Place below the trigger and within the viewport, the way the inspector's
  // own popover does; the panel is mouse-transparent (pointer-events:none),
  // so exact fit matters less than never covering the pointer.
  useLayoutEffect(() => {
    if (!open || !trigger.current) return;
    const rect = trigger.current.getBoundingClientRect();
    const width = 340;
    const left = Math.max(12, Math.min(rect.right - 8, window.innerWidth - width - 12));
    const below = rect.bottom + 6;
    const top =
      below + 120 < window.innerHeight - 12 ? below : Math.max(12, rect.top - 130);
    setPosition({ left, top });
  }, [open]);
  return (
    <span
      className="nf-label-tip"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      <button
        ref={trigger}
        type="button"
        className="nf-tip-trigger"
        aria-label={text}
        aria-expanded={open}
        onClick={(event) => {
          event.preventDefault();
          setOpen((value) => !value);
        }}
      >
        <Info size={13} />
      </button>
      {open &&
        createPortal(
          <div
            role="tooltip"
            className="nf-tip-popover"
            style={{ left: position.left, top: position.top }}
          >
            {text}
          </div>,
          document.body
        )}
    </span>
  );
}

export function FormField({
  label,
  required,
  hint,
  tip,
  children,
  error,
  meta,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  /** Extended help, shown on hover next to the label rather than inline. */
  tip?: string;
  children: ReactNode;
  error?: string;
  meta?: ReactNode;
}) {
  const id = useId();
  return (
    <div
      className="nf-field"
      role="group"
      aria-labelledby={`${id}-label`}
      aria-describedby={hint ? `${id}-hint` : undefined}
    >
      <div className="nf-label-row">
        <span id={`${id}-label`} className="nf-label">
          {label}
          {required && <span className="nf-required"> *</span>}
        </span>
        {meta || (tip ? <LabelTip text={tip} /> : undefined)}
      </div>
      {children}
      {hint && (
        <p id={`${id}-hint`} className="nf-hint">
          {hint}
        </p>
      )}
      {error && (
        <p role="alert" className="nf-error">
          {errorText(error)}
        </p>
      )}
    </div>
  );
}
export function FormSection({
  title,
  children,
  summary,
}: {
  title: string;
  children: ReactNode;
  summary?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className="nf-section">
      <button
        type="button"
        className="nf-section-trigger"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
      >
        <ChevronRight size={14} />
        <span>{title}</span>
        <span className="nf-section-summary">{summary}</span>
      </button>
      <Collapse open={open} id={id}>
        <div className="nf-section-content">{children}</div>
      </Collapse>
    </div>
  );
}
export { Select } from '../ui/select';
export function StatusLabel({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <span className={`nf-status ${error ? 'nf-error' : ''}`}>{children}</span>;
}
