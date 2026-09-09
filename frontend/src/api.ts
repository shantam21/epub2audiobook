export type JobState = "idle" | "running" | "done" | "failed";

export interface ChapterRow {
  idx: number;
  title: string;
  selected: boolean;
  prep: string;
  status: string;
  duration: number | null;
  chunks: number;
  chunks_done: number;
}

export interface Job {
  id: string;
  title: string;
  author: string;
  state: JobState;
  chapters: number;
  chapters_done: number;
  chunks: number;
  chunks_done: number;
  chunks_failed: number;
  audio_seconds: number;
  voice: string;
  speed: number;
  output: string | null;
  updated_at: number;
  error: string | null;
  percent: number;
  chapter_rows: ChapterRow[];
  log?: string;
}

export interface InspectChapter {
  idx: number;
  title: string;
  chars: number;
  estimated_seconds: number;
}

export interface Inspection {
  id?: string;
  title: string;
  author: string;
  has_cover: boolean;
  chapters: InspectChapter[];
  total_chars: number;
  estimated_audio_seconds: number;
  estimated_render_seconds: number;
}

export interface Voice {
  id: string;
  description: string;
}

export interface Meta {
  voices: Voice[];
  suggested_workers: number;
  threads_per_worker: number;
  data_dir: string;
}

export interface StartOptions {
  voice: string;
  speed: number;
  only: string | null;
  workers: number;
  llm: boolean;
  keep_front_matter: boolean;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) message = body.detail;
    } catch {
      /* the response had no JSON body; the status line is all we have */
    }
    throw new Error(message);
  }
  return res.json() as Promise<T>;
}

export const api = {
  meta: () => fetch("/api/voices").then(json<Meta>),

  listJobs: () => fetch("/api/jobs").then(json<{ jobs: Job[] }>).then((d) => d.jobs),

  getJob: (id: string) => fetch(`/api/jobs/${encodeURIComponent(id)}`).then(json<Job>),

  upload: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return fetch("/api/jobs", { method: "POST", body }).then(json<Inspection>);
  },

  start: (id: string, options: StartOptions) =>
    fetch(`/api/jobs/${encodeURIComponent(id)}/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options),
    }).then(json<{ ok: boolean }>),

  stop: (id: string) =>
    fetch(`/api/jobs/${encodeURIComponent(id)}/stop`, { method: "POST" }).then(
      json<{ stopped: boolean }>,
    ),

  remove: (id: string) =>
    fetch(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }).then(
      json<{ ok: boolean }>,
    ),

  downloadUrl: (id: string) => `/api/jobs/${encodeURIComponent(id)}/download`,

  /** Live progress. Falls back to polling if the stream drops. */
  subscribe(id: string, onJob: (job: Job) => void): () => void {
    const source = new EventSource(`/api/jobs/${encodeURIComponent(id)}/events`);
    let poll: number | undefined;

    source.onmessage = (event) => {
      try {
        onJob(JSON.parse(event.data) as Job);
      } catch {
        /* ignore a malformed frame rather than tearing down the stream */
      }
    };

    source.onerror = () => {
      // The server closes the stream when a job finishes, which surfaces as an
      // error. Poll once to settle on the final state, then stop.
      source.close();
      if (poll === undefined) {
        poll = window.setInterval(async () => {
          try {
            const job = await api.getJob(id);
            onJob(job);
            if (job.state === "done" || job.state === "failed") {
              window.clearInterval(poll);
              poll = undefined;
            }
          } catch {
            window.clearInterval(poll);
            poll = undefined;
          }
        }, 3000);
      }
    };

    return () => {
      source.close();
      if (poll !== undefined) window.clearInterval(poll);
    };
  },
};

export function formatDuration(seconds: number): string {
  const s = Math.round(seconds || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

export function formatNumber(n: number): string {
  return n.toLocaleString();
}
