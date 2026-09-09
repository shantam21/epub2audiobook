import { useCallback, useEffect, useRef, useState } from "react";
import type { Inspection, Job, Meta } from "./api";
import { api, formatDuration } from "./api";
import {
  ChapterList,
  DropZone,
  InspectSummary,
  JobRow,
  ProgressBar,
  Stat,
  StateBadge,
} from "./components";

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  const [voice, setVoice] = useState("af_heart");
  const [speed, setSpeed] = useState(1);
  const [only, setOnly] = useState("");
  const [workers, setWorkers] = useState(0);
  const [llm, setLlm] = useState(false);

  const unsubscribe = useRef<(() => void) | null>(null);
  // Which job's saved settings we have already loaded into the form, so a
  // live progress update never clobbers what the user is currently typing.
  const syncedFrom = useRef<string | null>(null);

  const refreshJobs = useCallback(async () => {
    try {
      setJobs(await api.listJobs());
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  }, []);

  useEffect(() => {
    api.meta().then(setMeta).catch((e) => setError(String(e.message ?? e)));
    refreshJobs();
    const timer = window.setInterval(refreshJobs, 10000);
    return () => window.clearInterval(timer);
  }, [refreshJobs]);

  // Adopt an existing job's voice and speed the first time we see it.
  // Resuming with different settings would invalidate every rendered chunk and
  // silently re-narrate the whole book in another voice.
  useEffect(() => {
    if (!job || !job.id || syncedFrom.current === job.id) return;
    if (job.voice) {
      setVoice(job.voice);
      setSpeed(job.speed || 1);
      syncedFrom.current = job.id;
    }
  }, [job]);

  // Live progress for whichever job is open.
  useEffect(() => {
    unsubscribe.current?.();
    unsubscribe.current = null;
    if (!selected) {
      setJob(null);
      return;
    }
    api.getJob(selected).then(setJob).catch(() => undefined);
    unsubscribe.current = api.subscribe(selected, (next) => {
      setJob(next);
      setJobs((prev) =>
        prev.map((j) => (j.id === next.id ? { ...j, ...next } : j)),
      );
    });
    return () => {
      unsubscribe.current?.();
      unsubscribe.current = null;
    };
  }, [selected]);

  const openJob = useCallback(async (id: string) => {
    setSelected(id);
    setError(null);
    try {
      setInspection(await fetch(`/api/jobs/${encodeURIComponent(id)}/inspect`).then((r) =>
        r.ok ? r.json() : null,
      ));
    } catch {
      setInspection(null);
    }
  }, []);

  const handleFile = useCallback(
    async (file: File) => {
      setError(null);
      setUploading(true);
      try {
        const result = await api.upload(file);
        setInspection(result);
        if (result.id) {
          setSelected(result.id);
          await refreshJobs();
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setUploading(false);
      }
    },
    [refreshJobs],
  );

  const start = useCallback(async () => {
    if (!selected) return;
    setError(null);
    try {
      await api.start(selected, {
        voice,
        speed,
        only: only.trim() || null,
        workers,
        llm,
        keep_front_matter: false,
      });
      const fresh = await api.getJob(selected);
      setJob(fresh);
      unsubscribe.current?.();
      unsubscribe.current = api.subscribe(selected, setJob);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [selected, voice, speed, only, workers, llm]);

  const stop = useCallback(async () => {
    if (!selected) return;
    try {
      await api.stop(selected);
      setJob(await api.getJob(selected));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [selected]);

  const remove = useCallback(async () => {
    if (!selected) return;
    if (!confirm("Delete this job and all of its audio? This cannot be undone.")) return;
    try {
      await api.remove(selected);
      setSelected(null);
      setInspection(null);
      await refreshJobs();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [selected, refreshJobs]);

  const running = job?.state === "running";

  return (
    <div className="app">
      <header className="masthead">
        <h1>epub2audiobook</h1>
        <p>
          Convert an EPUB into a chaptered M4B with local Kokoro narration.
          Conversions resume, so you can stop any time.
        </p>
      </header>

      {error && <div className="error">{error}</div>}

      <section className="panel">
        <h2>Add a book</h2>
        <DropZone onFile={handleFile} busy={uploading} />
        {meta && (
          <p className="hint">
            Jobs are stored in <code>{meta.data_dir}</code>. This machine suggests{" "}
            {meta.suggested_workers} worker
            {meta.suggested_workers === 1 ? "" : "s"} &times; {meta.threads_per_worker} threads.
          </p>
        )}
      </section>

      {jobs.length > 0 && (
        <section className="panel">
          <h2>Books</h2>
          {jobs.map((j) => (
            <JobRow
              key={j.id}
              job={j}
              active={j.id === selected}
              onSelect={() => openJob(j.id)}
            />
          ))}
        </section>
      )}

      {selected && (
        <section className="panel">
          <h2>{job?.title || inspection?.title || selected}</h2>

          {inspection && !job?.chunks && (
            <InspectSummary
              chapters={inspection.chapters.length}
              totalChars={inspection.total_chars}
              audioSeconds={inspection.estimated_audio_seconds}
              renderSeconds={inspection.estimated_render_seconds}
            />
          )}

          {job && job.chunks > 0 && (
            <>
              <div className="progress-head">
                <StateBadge state={job.state} />
                <span className="spacer" />
                <span className="pct">{job.percent.toFixed(1)}%</span>
              </div>
              <ProgressBar percent={job.percent} state={job.state} />
              <div className="stats">
                <Stat label="Chapters" value={`${job.chapters_done} / ${job.chapters}`} />
                <Stat label="Chunks" value={`${job.chunks_done} / ${job.chunks}`} />
                <Stat label="Audio rendered" value={formatDuration(job.audio_seconds)} />
                {job.chunks_failed > 0 && (
                  <Stat label="Failed" value={String(job.chunks_failed)} />
                )}
              </div>
            </>
          )}

          <div className="row" style={{ marginTop: 20 }}>
            <div className="field">
              <label htmlFor="voice">Voice</label>
              <select
                id="voice"
                value={voice}
                onChange={(e) => setVoice(e.target.value)}
                disabled={running}
              >
                {meta?.voices.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.id} — {v.description}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label htmlFor="speed">Speed</label>
              <input
                id="speed"
                type="number"
                min={0.5}
                max={2}
                step={0.05}
                value={speed}
                onChange={(e) => setSpeed(Number(e.target.value))}
                disabled={running}
                style={{ minWidth: 90 }}
              />
            </div>

            <div className="field">
              <label htmlFor="only">Chapters</label>
              <input
                id="only"
                type="text"
                placeholder="all — or 0-5,9"
                value={only}
                onChange={(e) => setOnly(e.target.value)}
                disabled={running}
              />
            </div>

            <div className="field">
              <label htmlFor="workers">Workers</label>
              <input
                id="workers"
                type="number"
                min={0}
                max={8}
                value={workers}
                onChange={(e) => setWorkers(Number(e.target.value))}
                disabled={running}
                style={{ minWidth: 90 }}
              />
            </div>
          </div>

          <div className="row" style={{ marginTop: 16 }}>
            <label className="check">
              <input
                type="checkbox"
                checked={llm}
                onChange={(e) => setLlm(e.target.checked)}
                disabled={running}
              />
              Claude text pre-pass (needs ANTHROPIC_API_KEY)
            </label>

            <span className="spacer" />

            {running ? (
              <button className="danger" onClick={stop}>Stop</button>
            ) : (
              <button className="primary" onClick={start}>
                {job && job.chunks_done > 0 ? "Resume" : "Convert"}
              </button>
            )}

            {job?.output && (
              <a href={api.downloadUrl(job.id)} download>
                <button className="primary">Download M4B</button>
              </a>
            )}

            <button className="danger" onClick={remove} disabled={running}>Delete</button>
          </div>

          <p className="hint">
            Workers <code>0</code> picks a count from your CPU and free memory. Each worker
            holds its own copy of the model, so more is not always faster.
          </p>

          {job && job.chapter_rows.length > 0 && (
            <>
              <h2 style={{ marginTop: 24 }}>Chapters</h2>
              <ChapterList rows={job.chapter_rows} />
            </>
          )}

          {inspection && !job?.chapter_rows.length && (
            <>
              <h2 style={{ marginTop: 24 }}>Chapters</h2>
              <div className="chapters">
                {inspection.chapters.map((c) => (
                  <div key={c.idx} className="chapter">
                    <span className="idx">{c.idx}</span>
                    <span className="title" title={c.title}>{c.title}</span>
                    <span className="count">{c.chars.toLocaleString()} chars</span>
                    <span className="count">{formatDuration(c.estimated_seconds)}</span>
                  </div>
                ))}
              </div>
            </>
          )}

          {job?.error && <div className="error" style={{ marginTop: 16 }}>{job.error}</div>}
          {job?.log && <pre className="log">{job.log}</pre>}
        </section>
      )}
    </div>
  );
}
