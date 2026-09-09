import { useCallback, useRef, useState } from "react";
import type { Job } from "./api";
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

/** One row of the chapter picker, normalised from either source. */
export interface ChapterItem {
  idx: number;
  title: string;
  /** Progress fields, present once a conversion has planned the chapter. */
  chunks?: number;
  chunks_done?: number;
  status?: string;
  duration?: number | null;
  /** Estimate, shown before anything has been rendered. */
  estimated_seconds?: number;
}

export function ChapterList({
  rows,
  chosen,
  onToggle,
  disabled,
}: {
  rows: ChapterItem[];
  chosen: Set<number>;
  onToggle: (idx: number) => void;
  disabled: boolean;
}) {
  if (!rows.length) return <div className="empty">No chapters found.</div>;
  return (
    <div className="chapters">
      {rows.map((c) => {
        const on = chosen.has(c.idx);
        const planned = (c.chunks ?? 0) > 0;
        const pct = planned ? (100 * (c.chunks_done ?? 0)) / (c.chunks as number) : 0;
        return (
          <label key={c.idx} className={`chapter ${on ? "" : "off"}`}>
            <input
              type="checkbox"
              checked={on}
              disabled={disabled}
              onChange={() => onToggle(c.idx)}
              aria-label={`Include ${c.title}`}
            />
            <span className="title" title={c.title}>
              <span className="idx">{c.idx}</span>
              {c.title}
            </span>
            {on && planned ? (
              <ProgressBar percent={pct} state={c.status === "done" ? "done" : ""} slim />
            ) : (
              <span className="count">{on ? "" : "skipped"}</span>
            )}
            <span className="count">
              {c.status === "done" && c.duration
                ? formatDuration(c.duration)
                : planned && on
                  ? `${c.chunks_done}/${c.chunks}`
                  : c.estimated_seconds
                    ? formatDuration(c.estimated_seconds)
                    : ""}
            </span>
          </label>
        );
      })}
    </div>
  );
}

/** Turn a set of chapter indices into the CLI's --only syntax: "0-5,9". */
export function toRangeString(chosen: Set<number>, all: number[]): string | null {
  if (all.every((i) => chosen.has(i))) return null; // null means "everything"
  const sorted = [...chosen].sort((a, b) => a - b);
  const parts: string[] = [];
  let start: number | null = null;
  let prev: number | null = null;
  for (const n of sorted) {
    if (start === null) {
      start = prev = n;
      continue;
    }
    if (prev !== null && n === prev + 1) {
      prev = n;
      continue;
    }
    parts.push(start === prev ? `${start}` : `${start}-${prev}`);
    start = prev = n;
  }
  if (start !== null) parts.push(start === prev ? `${start}` : `${start}-${prev}`);
  return parts.join(",");
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
