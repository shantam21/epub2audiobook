import { useCallback, useRef, useState } from "react";
import type { ChapterRow, Job } from "./api";
import { formatDuration, formatNumber } from "./api";

export function ProgressBar({
  percent,
  state,
  slim,
}: {
  percent: number;
  state?: string;
  slim?: boolean;
}) {
  const cls = ["bar", slim ? "slim" : "", state === "done" ? "done" : "",
    state === "failed" ? "failed" : ""].filter(Boolean).join(" ");
  return (
    <div
      className={cls}
      role="progressbar"
      aria-valuenow={Math.round(percent)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <i style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} />
    </div>
  );
}

export function StateBadge({ state }: { state: string }) {
  const label =
    state === "running" ? "converting" :
    state === "done" ? "finished" :
    state === "failed" ? "failed" : "paused";
  return <span className={`badge ${state}`}>{label}</span>;
}

export function DropZone({
  onFile,
  busy,
}: {
  onFile: (file: File) => void;
  busy: boolean;
}) {
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const take = useCallback(
    (files: FileList | null) => {
      const file = files?.[0];
      if (file) onFile(file);
    },
    [onFile],
  );

  return (
    <div
      className={`dropzone ${over ? "over" : ""}`}
      onClick={() => !busy && input.current?.click()}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        if (!busy) take(e.dataTransfer.files);
      }}
    >
      <strong>{busy ? "Reading the book…" : "Drop an EPUB here"}</strong>
      <span>{busy ? "Parsing chapters" : "or click to choose a file"}</span>
      <input
        ref={input}
        type="file"
        accept=".epub,application/epub+zip"
        hidden
        onChange={(e) => take(e.target.files)}
      />
    </div>
  );
}

export function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

export function JobRow({
  job,
  active,
  onSelect,
}: {
  job: Job;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <div
      className={`job ${active ? "active" : ""}`}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect()}
    >
      <div className="grow">
        <div className="name">{job.title || job.id}</div>
        <div className="meta">
          {job.author || "Unknown author"} · {job.chapters_done}/{job.chapters} chapters
          {job.audio_seconds > 0 && ` · ${formatDuration(job.audio_seconds)}`}
        </div>
        <div style={{ marginTop: 8 }}>
          <ProgressBar percent={job.percent} state={job.state} slim />
        </div>
      </div>
      <StateBadge state={job.state} />
    </div>
  );
}

export function ChapterList({ rows }: { rows: ChapterRow[] }) {
  if (!rows.length) return <div className="empty">No chapters planned yet.</div>;
  return (
    <div className="chapters">
      {rows.map((c) => {
        const pct = c.chunks ? (100 * c.chunks_done) / c.chunks : 0;
        return (
          <div key={c.idx} className={`chapter ${c.selected ? "" : "off"}`}>
            <span className="idx">{c.idx}</span>
            <span className="title" title={c.title}>{c.title}</span>
            {c.selected ? (
              <ProgressBar percent={pct} state={c.status === "done" ? "done" : ""} slim />
            ) : (
              <span className="count">skipped</span>
            )}
            <span className="count">
              {c.status === "done" && c.duration
                ? formatDuration(c.duration)
                : c.selected
                  ? `${c.chunks_done}/${c.chunks}`
                  : ""}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function InspectSummary({
  chapters,
  totalChars,
  audioSeconds,
  renderSeconds,
}: {
  chapters: number;
  totalChars: number;
  audioSeconds: number;
  renderSeconds: number;
}) {
  return (
    <div className="stats">
      <Stat label="Chapters" value={String(chapters)} />
      <Stat label="Characters" value={formatNumber(totalChars)} />
      <Stat label="Audiobook length" value={formatDuration(audioSeconds)} />
      <Stat label="Time to render" value={`~${formatDuration(renderSeconds)}`} />
    </div>
  );
}
