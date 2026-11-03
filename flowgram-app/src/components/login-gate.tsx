/**
 * Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
 * SPDX-License-Identifier: MIT
 */

import { useEffect, useState, type FormEvent } from 'react';

import { Loader2, LogIn, TriangleAlert } from 'lucide-react';

import { api } from '../lib/api';
import { tx } from '../lib/i18n';
import { Button } from './ui/button';

interface SessionInfo {
  auth_required: boolean;
  username: string | null;
  expires_at?: number | null;
}

/**
 * The login gate.
 *
 * Credentials are checked against the host's sshd, not a user table: this app
 * has no accounts of its own, and the machine it runs on already knows who may
 * use it. The password is forwarded once and never stored - the agent issues an
 * httpOnly session cookie and the browser never sees it again.
 *
 * The gate is a *probe*, not an assumption: `/api/session` is public and says
 * whether a login is required at all. An agent started with `--no-auth` answers
 * `auth_required: false`, and this component renders nothing.
 */
export function LoginGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<'probing' | 'open' | 'closed'>('probing');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [who, setWho] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    void api<SessionInfo>('/api/session')
      .then((session) => {
        if (disposed) return;
        setWho(session.username);
        setState(session.auth_required ? 'closed' : 'open');
      })
      .catch((cause) => {
        if (disposed) return;
        // 401 means "a login is required and none is present" - which is the
        // normal first visit, not an error to report.
        const message = cause instanceof Error ? cause.message : '';
        if (/\b401\b|login required/i.test(message)) {
          setState('closed');
        } else {
          setError(message);
          setState('closed');
        }
      });
    return () => {
      disposed = true;
    };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !username.trim()) return;
    setBusy(true);
    setError('');
    try {
      const session = await api<SessionInfo & { username: string }>('/api/login', 'POST', {
        username: username.trim(),
        password,
      });
      setWho(session.username);
      setPassword('');
      setState('open');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  // While probing, render nothing rather than flashing the form at a user who
  // is already signed in.
  if (state === 'probing') return null;
  if (state === 'open') return <>{children}</>;

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="login-heading">
          <span className="login-mark" aria-hidden="true">
            <LogIn size={18} />
          </span>
          <div>
            <h1>{tx('登录')}</h1>
            <p>{tx('使用这台机器上的账户。密码交给 sshd 校验，不会被保存。')}</p>
          </div>
        </div>

        <label className="login-field">
          <span>{tx('用户名')}</span>
          <input
            autoFocus
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={busy}
          />
        </label>
        <label className="login-field">
          <span>{tx('密码')}</span>
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={busy}
          />
        </label>

        {error && (
          <p role="alert" className="login-error">
            <TriangleAlert size={13} />
            {error}
          </p>
        )}

        <Button type="submit" disabled={busy || !username.trim()}>
          {busy ? <Loader2 className="animate-spin" /> : <LogIn />}
          {busy ? tx('正在验证') : tx('登录')}
        </Button>

        {who && (
          <p className="login-note">
            {tx('当前会话：{0}', [who])}
          </p>
        )}
      </form>
    </div>
  );
}

/** Sign out, for the settings page. */
export function LogoutButton() {
  const [busy, setBusy] = useState(false);
  return (
    <Button
      variant="outline"
      disabled={busy}
      onClick={() => {
        setBusy(true);
        void api('/api/logout', 'POST', {})
          .catch(() => undefined)
          .finally(() => window.location.reload());
      }}
    >
      {tx('退出登录')}
    </Button>
  );
}
