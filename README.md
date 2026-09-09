# epub2audiobook

Turn an EPUB into a chaptered `.m4b` audiobook that drops straight into Apple
Books. Narration is generated locally with [Kokoro-82M][kokoro] — no API keys,
no per-book cost, no upload of the book anywhere.

Conversions are **resumable**, which matters because they are slow: on CPU with
no GPU, Kokoro renders at roughly **1.15× the finished audiobook's length**, so
a five-hour book takes about six hours to generate. You can stop it with Ctrl+C,
reboot, and pick up exactly where you left off — down to the individual chunk.
`epub2ab inspect` estimates both numbers for your specific book before you
start.

```bash
uv run epub2ab serve                           # web UI at localhost:8000
```

or from the terminal:

```bash
uv run epub2ab inspect book.epub                    # what's in it, how long it'll run
uv run epub2ab sample  book.epub --voice bm_george  # audition a narrator first
uv run epub2ab convert book.epub --voice af_heart   # convert (re-run to resume)
uv run epub2ab status  ./book                       # how far along am I
uv run epub2ab resume  ./book                       # continue after an interruption
```

## The web UI

`epub2ab serve` opens a browser app: drop in an EPUB, see its chapters and how
long the audiobook will run, pick a voice, and watch per-chapter progress bars
fill in. When it finishes, download the `.m4b` from the same page.

```bash
uv run epub2ab serve --data-dir ~/audiobooks
```

The server never loads the TTS model. It launches the CLI as a subprocess and
reads each job's SQLite database, so the page stays responsive while several
hundred megabytes of Kokoro run elsewhere, and a crashing render can't take the
UI down. Because progress comes from that same database, **a conversion started
in the terminal shows up in the browser and vice versa** — they are the same job,
not two systems.

Progress streams over server-sent events, falling back to polling if the stream
drops.

The built front end is committed to the repository so that
`pip install git+https://github.com/shantam21/epub2audiobook` gives you a
working UI without needing Node. To change it:

```bash
cd frontend
npm install
npm run dev     # Vite dev server, proxying /api to :8000
npm run build   # writes into src/epub2audiobook/server/static/
```

## Install

The project uses [uv](https://docs.astral.sh/uv/). It resolves and installs the
whole dependency tree — PyTorch included — in about two minutes from a cold
cache, and it fetches the right Python for you, so there is no separate Python
install step.

```bash
winget install astral-sh.uv          # or: curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/shantam21/epub2audiobook
cd epub2audiobook
uv sync
```

That is the whole setup. `uv sync` reads `.python-version`, downloads CPython
3.12 if you don't have it, creates `.venv`, and installs everything from
`uv.lock` at the exact pinned versions — including the web UI.

The optional Claude pre-pass is the one thing kept out of the default install,
since it is only useful with an API key: `uv sync --extra llm`.

On Windows PowerShell, you can activate the environment explicitly with:

```powershell
.venv\Scripts\Activate.ps1
```

If PowerShell blocks the activation script, allow locally created scripts once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

For Command Prompt (`cmd.exe`), use `.venv\Scripts\activate.bat` instead.
When the environment is active, `(.venv)` appears at the start of your prompt.
Leave it with `deactivate`.

Then run commands through `uv run`, which needs no activated shell:

```bash
uv run epub2ab serve
uv run epub2ab convert book.epub
uv run pytest
```

**Why Python 3.12 and not newer:** Kokoro depends on PyTorch, which has no
wheels for 3.13+. On 3.14, pip falls back to compiling NumPy from source and
fails. `requires-python` and `.python-version` pin this so you can't hit it.

### One other thing you need

**ffmpeg**, used to encode and mux the final `.m4b`:

```bash
winget install Gyan.FFmpeg        # macOS: brew install ffmpeg
```

**On Windows, also check your Visual C++ runtime.** PyTorch needs 14.40 or
newer; a machine that only ever had Visual Studio 2017 will be on 14.13 and
`import torch` dies with a `c10.dll` initialization error:

```bash
winget install Microsoft.VCRedist.2015+.x64
```

Open a new terminal afterwards so `PATH` picks everything up.

### Installing it as a tool

To use `epub2ab` anywhere without cloning:

```bash
uv tool install "epub2audiobook @ git+https://github.com/shantam21/epub2audiobook"
```

### Without uv

`pip install -e ".[llm]"` still works on a Python 3.10–3.12 environment you
made yourself. It is just considerably slower, and you get no lockfile.

The first `convert` or `sample` pulls two things the lockfile cannot cover,
because they are model data rather than packages: the Kokoro weights (~350 MB)
into the Hugging Face cache, and spaCy's `en_core_web_sm` (~12 MB), which
Kokoro's text frontend fetches itself. Both happen once, and both need network
access on that first run.

Kokoro falls back to **espeak-ng** for words outside its dictionary; the
`espeakng-loader` package bundled with it supplies that on Windows, so no
separate install is normally needed. If model loading does complain about a
missing backend, install it from the [espeak-ng releases page][espeak].

## How it works

```
EPUB ──▶ chapters ──▶ cleanup ──▶ [Claude pre-pass] ──▶ chunks ──▶ Kokoro ──▶ M4B
          spine       dehyphenate      optional         sentence     24 kHz    AAC +
          order       de-page-number                    boundaries    mono     chapters
```

**Chapters** come from the spine (true reading order), with titles taken from
the table of contents and falling back to the first heading. Front matter under
250 characters is skipped; `--keep-front-matter` keeps it.

**Cleanup** is deterministic and always runs: rejoins words hyphenated across
line breaks, deletes page numbers and running heads, strips footnote markers and
scene-break rules, and — importantly — collapses the newlines that print layout
leaves inside paragraphs, since the synthesiser treats a newline as a pause.

**Chunking** cuts text into ~380-character pieces, always on a sentence
boundary. Kokoro re-derives prosody per chunk, so a mid-sentence cut is
audible; abbreviations (`Dr.`, `St.`, `p.m.`) and initials (`J. R. R.`) are held
back from being mistaken for sentence ends. Paragraph breaks survive as newlines
inside a chunk so the pause lands where a reader would take one.

**Synthesis** runs Kokoro at 24 kHz mono, trimming the model's own padding from
each chunk so the gaps between them are the ones you configured rather than
whatever came out.

**Assembly** concatenates chunks into chapter WAVs, then muxes everything into
one `.m4b` with chapter markers, cover art from the EPUB, and
`media_type=2` — the tag that makes Apple Books shelve it as an audiobook
rather than music.

## Speed and parallelism

Kokoro on CPU does not saturate a modern machine by itself — torch's internal
threading tops out around two or three cores, leaving the rest idle through a
multi-hour book. `--workers N` runs several synthesis processes at once:

```bash
epub2ab convert book.epub --workers 3
```

`--workers 0` (the default) picks a count from your logical core count **and
your free RAM**, because RAM is usually the real limit: each worker holds its
own copy of the model and lands around 1–1.5 GB resident. Starting more workers
than fit makes the machine swap, which is slower than running one. If a
conversion is going slowly, check free memory before adding workers.

Only synthesis is parallel. Every database write still happens in the parent
process, so SQLite keeps exactly one writer and the resume guarantee below is
unchanged.

### If it suddenly gets slow

Free memory is measured when the run *starts*, but a render takes hours and
other applications grow in the meantime. If memory gets tight, the OS trims the
workers' resident sets and they begin paging — throughput collapses by roughly
an order of magnitude with no error message and no failed chunks.

Observed on a 10 GB machine: two workers held ~6.5 chunks/min with 3.5 GB free,
then fell to ~0.9 chunks/min once an editor and a browser had taken free memory
down to 0.9 GB. The fix is to stop and restart with fewer workers:

```bash
epub2ab resume ./book --workers 1
```

Nothing is lost — the restart resumes from the last finished chunk. One worker
that stays resident beats two that are swapping.

## Resuming

Every chapter's text prep and every individual TTS chunk is a row in a SQLite
database (`<workdir>/job.db`) with its own status. Chunk WAVs are written to a
temp name and renamed into place, so a half-written file is never marked done.
Kill the process at any point and you lose at most one chunk.

Each chunk is keyed by a hash of its text **and** every setting that affects the
audio. So:

- Re-running after an interruption reuses everything already rendered.
- Changing `--voice` or `--speed` correctly re-renders the book instead of
  splicing two narrators together.
- Editing the EPUB re-renders only the chapters whose text actually changed.

```bash
epub2ab convert book.epub          # ^C after two hours
epub2ab resume ./book              # picks up mid-chapter
epub2ab resume ./book --retry-failed   # re-queue anything that errored
```

## The Claude pre-pass, by hand (no API key)

If you'd rather use your Claude subscription than an API key, `export-text`
writes each chapter out as a file to paste into the app, and `import-text` folds
the replies back in. Same benefit, no key, no per-token cost — you pay in
pasting.

```bash
epub2ab export-text book.epub          # writes ./book/prep/
# ... paste in the Claude app, save each reply ...
epub2ab import-text ./book
epub2ab convert book.epub -o ./book    # uses the imported text
```

`export-text` creates a `prep/` folder containing:

```
PROMPT.txt          paste this once, as your first message
README.txt          these instructions, next to the files
ch0000-01.in.txt    chapter 0, part 1  ── paste this
ch0000-02.in.txt    chapter 0, part 2
ch0001-01.in.txt    ...
```

Chapters are split at paragraph boundaries into ~12,000-character parts so a
single reply never hits the app's output limit. For each `.in.txt`, paste it,
then save Claude's reply beside it as the matching `.out.txt`
(`ch0000-01.in.txt` → `ch0000-01.out.txt`).

Do it all in **one conversation** — Claude stays consistent about how it handles
recurring names and numbers across chapters, which a fresh conversation per
chapter loses.

You don't have to finish. `import-text` takes whatever is ready; anything
without an `.out.txt` keeps the automatic cleanup, and you can import again
later as you do more. It applies the same length check as the API path and
refuses a chapter that came back looking summarised (`--force` overrides).

`epub2ab prompt` prints the prompt on its own if you just want to copy it.

## The Claude pre-pass, automatic (API key)

`--llm` sends each chapter through the Claude API to rewrite it *for the ear*
before synthesis. This is what turns `In 1885, Dr. Ashworth-Vane paid $4.50` into
`In eighteen eighty-five, Doctor Ashworth-Vane paid four dollars and fifty
cents`. It also expands abbreviations and symbols, spaces out initialisms
spoken as letters (`FBI` → `F B I`) while leaving ones spoken as words alone
(`NASA`), converts roman ordinals (`Henry VIII` → `Henry the Eighth`), and
removes print artefacts the regex pass can't safely judge.

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # or run: ant auth login
epub2ab convert book.epub --llm
```

It defaults to `claude-opus-5` at low effort — roughly **$3–5 for a full novel**.
`epub2ab inspect` prints an estimate for your specific book before you commit.
Use `--llm-model claude-sonnet-5` or `claude-haiku-4-5` to spend less.

The pass is deliberately paranoid. Output is length-checked against input, and
any chapter that comes back suspiciously short, refused, or truncated falls back
to the deterministic cleanup. A summarised chapter would be invisible until you
were three hours into listening, so it is never allowed through. A chapter whose
pre-pass fails is logged and converted with cleaned text — one bad API call
never costs you the book.

## Getting it into Apple Books

The output is `<workdir>/<Book Title>.m4b`.

- **macOS / iOS:** open Books and drag the `.m4b` in, or **File ▸ Add to
  Library**. It appears under *Audiobooks* and syncs to your other devices via
  iCloud.
- **Windows:** there's no Books app, so put the file in iCloud Drive (or
  AirDrop / email it) and add it from your Mac, iPhone, or iPad.

Apple Books remembers your listening position per file and uses the embedded
chapter markers for its chapter list and scrubber.

## Commands

| Command | What it does |
|---|---|
| `inspect BOOK.epub` | Chapter list, character counts, estimated audio length, LLM cost estimate |
| `sample BOOK.epub` | Render ~45 seconds so you can audition a voice |
| `convert BOOK.epub` | Full conversion; re-run to resume |
| `resume WORKDIR` | Continue with the job's saved settings |
| `status WORKDIR` | Per-chapter progress table |
| `export-text BOOK.epub` | Write chapters out to paste into the Claude app |
| `import-text WORKDIR` | Fold the pasted replies back in |
| `prompt` | Print the Claude-app prompt |
| `serve` | Run the web UI |
| `voices` | Available Kokoro narrators |

Useful `convert` flags:

| Flag | Default | Notes |
|---|---|---|
| `--voice` | `af_heart` | See `epub2ab voices` |
| `--speed` | `1.0` | 0.9 is noticeably more relaxed for long listening |
| `--llm` | off | The Claude pre-pass |
| `--only` | all | `--only 0-5,9` converts a subset |
| `--bitrate` | `64k` | Plenty for 24 kHz mono speech |
| `--max-chunk-chars` | `380` | Lower is safer, slower, and adds more seams |
| `--workers` | auto | Parallel synthesis processes; auto reads cores and free RAM |
| `--audio-only` | off | Render chapter WAVs, skip the M4B mux |
| `--keep-work` | off | Keep the intermediate WAVs after the M4B is built |
| `--keep-front-matter` | off | Keep title page, TOC, copyright |

## Project layout

```
src/epub2audiobook/
  cli.py          commands, and the render loop
  epubsrc.py      EPUB -> ordered chapters
  textprep.py     cleanup and sentence-aware chunking
  manualprep.py   paste-into-Claude workflow
  llmprep.py      Claude API pre-pass
  tts.py          Kokoro synthesis
  render.py       parallel worker pool
  assemble.py     chapter WAVs -> chaptered M4B
  store.py        SQLite progress
  server/         FastAPI backend + built React UI
frontend/         React + TypeScript source (Vite)
tests/            114 tests
uv.lock           exact pinned versions for every dependency
.python-version   3.12, because PyTorch has no 3.13+ wheels
```

## Development

```bash
uv sync --all-extras     # or --frozen in CI, to fail on a stale lock
uv run pytest
uv add <package>         # updates pyproject.toml and uv.lock together
uv lock --upgrade        # refresh the lock
```

Note that `uv run` re-syncs the environment before every command, which prunes
anything not in the current dependency set. That is why the web UI is a real
dependency rather than an extra: as an extra, a plain `uv run epub2ab serve`
would have uninstalled FastAPI and then failed on the missing import.

CI runs the test suite on Linux and Windows with `--frozen`, so a dependency
change that was never locked cannot reach `main`, and rebuilds the front end to
check the committed bundle matches its source.

## Job directory layout

```
<workdir>/
  job.db                  progress and settings — delete to start over
  <Book Title>.m4b        the finished audiobook
  work/
    chunks/ch0000/*.wav   per-chunk audio (the resume checkpoint)
    chapters/ch0000.wav   assembled chapter audio
    chapters.ffmeta       chapter markers handed to ffmpeg
```

`work/` is deleted automatically once the `.m4b` is built, so a finished job is
the one file you wanted rather than a folder of scaffolding around it. Pass
`--keep-work` to keep the intermediates, at the cost of roughly **170 MB of WAV
per hour** of audiobook (a 12-hour novel peaks near 2 GB while converting).

Cleanup only happens on a *successful* mux — if the M4B step fails, everything
is kept so a re-run finishes in seconds rather than re-rendering.

[kokoro]: https://huggingface.co/hexgrad/Kokoro-82M
[espeak]: https://github.com/espeak-ng/espeak-ng/releases
