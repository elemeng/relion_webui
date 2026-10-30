import { useState, type FormEvent } from "react";
import { Spinner } from "@/components/ui/spinner";
import { AlertCircle, Microscope } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError } from "@/lib/api";

/**
 * Sign-in form. Credentials are checked by the host's sshd on the server side;
 * this component only relays them and reacts to the result.
 */
export function LoginPage({ onSignIn }: { onSignIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.post("/api/login", { username, password });
      // The session cookie is set; the shell picks up from here.
      onSignIn();
    } catch (caught) {
      const message =
        caught instanceof ApiError
          ? caught.message
          : "could not reach the server";
      setError(message);
      setPassword("");
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-svh items-center justify-center bg-canvas p-6">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center gap-2 text-center">
          <div className="grid size-11 place-items-center rounded-box bg-frame text-frame-ink">
            <Microscope className="size-6" />
          </div>
          <h1 className="text-xl font-semibold tracking-tight">
            RELION cryo-ET
          </h1>
          <p className="t-ui text-ink-muted">
            Sign in with your account on this machine
          </p>
        </div>

        <form
          onSubmit={onSubmit}
          className="flex flex-col gap-4 rounded-box border border-rule bg-base-100 p-6"
        >
          <div className="flex flex-col gap-2">
            <Label htmlFor="username">Username</Label>
            <Input
              id="username"
              name="username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              autoFocus
              required
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>

          {error && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-[4px] border border-[color-mix(in_srgb,var(--st-failed-text)_35%,var(--rule))] bg-[var(--st-failed-fill)] px-3 py-2 t-ui text-[var(--st-failed-text)]"
            >
              <AlertCircle className="mt-0.5 size-4 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <Button type="submit" disabled={busy} className="mt-1 w-full">
            {busy && <Spinner size="sm" />}
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="mt-6 text-center t-label text-ink-faint">
          Credentials are verified by this host&apos;s SSH server.
        </p>
      </div>
    </div>
  );
}
