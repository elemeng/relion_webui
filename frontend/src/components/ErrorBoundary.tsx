// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Catches a render error so one broken panel does not blank the whole app.
 *
 * Without this, a malformed MRC header or an unexpected shape from the API
 * throws inside a component and React unmounts the tree, leaving a white page
 * and no way to recover except reloading blind. The fallback keeps the message
 * and offers the reload, so the user can report what broke.
 */
export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // The console is the only log this bundle has; the trace is what makes a
    // report actionable.
    console.error("RELION web UI crashed:", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="bg-base-200 flex min-h-svh items-center justify-center p-6">
        <div className="bg-base-100 flex max-w-lg flex-col gap-3 rounded-xl border p-6 shadow-sm">
          <div className="text-error flex items-center gap-2">
            <AlertTriangle className="size-5" />
            <h1 className="text-sm font-semibold">Something went wrong</h1>
          </div>
          <p className="t-base-content/60 t-ui">
            The interface hit an error it could not recover from. Your jobs are
            unaffected: they run in the agent, not in this page.
          </p>
          <pre className="max-h-40 overflow-auto rounded-field border border-rule bg-base-200 p-2 font-mono t-label whitespace-pre-wrap">
            {error.message}
          </pre>
          <div className="flex gap-2">
            <Button size="sm" onClick={() => window.location.reload()}>
              <RotateCcw className="size-3.5" />
              Reload
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => this.setState({ error: null })}
            >
              Try again
            </Button>
          </div>
        </div>
      </div>
    );
  }
}
