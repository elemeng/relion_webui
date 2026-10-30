import { useCallback, useEffect, useRef, useState } from "react";

import type { Job, LogChunk } from "@/lib/types";

interface JobsSocketState {
  jobs: Job[];
  /** Appended when subscribed to one job's log. */
  logChunk: LogChunk | null;
}

/**
 * Live job state over the agent's WebSocket.
 *
 * The server pushes on a timer, and optionally a log chunk when a job is being
 * watched. Reconnects on close so an agent restart does not freeze the panel.
 */
export function useJobsSocket() {
  const [state, setState] = useState<JobsSocketState>({
    jobs: [],
    logChunk: null,
  });
  const socketRef = useRef<WebSocket | null>(null);
  const watchRef = useRef<{ id: string; offset: number } | null>(null);

  const connect = useCallback(() => {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${window.location.host}/ws/jobs`);
    socketRef.current = socket;

    socket.addEventListener("message", (event) => {
      let payload: {
        jobs?: Job[];
        log?: LogChunk;
      };
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      setState((previous) => ({
        jobs: payload.jobs ?? previous.jobs,
        logChunk: payload.log ?? null,
      }));
    });

    socket.addEventListener("open", () => {
      // Resume the log subscription after a reconnect, from where it stopped.
      const watch = watchRef.current;
      if (watch && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ watch: watch.id, offset: watch.offset }));
      }
    });

    socket.addEventListener("close", () => {
      socketRef.current = null;
      watchRef.current = null;
      window.setTimeout(connect, 2000);
    });
  }, []);

  useEffect(() => {
    connect();
    return () => {
      const socket = socketRef.current;
      socketRef.current = null;
      socket?.close();
    };
  }, [connect]);

  const watchLog = useCallback((jobId: string, offset: number) => {
    watchRef.current = { id: jobId, offset };
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ watch: jobId, offset }));
    }
  }, []);

  const unwatchLog = useCallback(() => {
    watchRef.current = null;
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ unwatch: true }));
    }
  }, []);

  return { ...state, watchLog, unwatchLog };
}
