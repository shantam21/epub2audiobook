# The Audiobook That Doesn't Exist

*I wanted to listen to a book nobody had recorded. Turning text into speech was the easy part.*

---

Some books never get an audiobook. Older titles, translations, technical texts, anything that isn't going to sell enough copies to justify a narrator and a studio. If you want to listen to those, you're out of luck.

So I built a converter: EPUB in, chaptered `.m4b` out, drops straight into Apple Books. It runs entirely on my laptop — no API keys, no per-book cost, and the book never leaves the machine. A 300-page memoir came out as five hours and eighteen minutes of audio for $0.

The naive version of this is about twenty lines of Python. Read the EPUB, hand the text to a text-to-speech model, write the file. That version works, in the sense that it produces a file. It's also unlistenable, and the reasons why turned out to be the actual project.

## The text is not ready to be spoken

Here's the first thing that broke, and it's the one I'd never have predicted.

A speech model re-derives its intonation for every chunk of text you hand it. So you have to cut the text somewhere, and where you cut is audible. Cut mid-sentence and the narrator stops dead and restarts with fresh, wrong emphasis. So: only cut on sentence boundaries. Easy enough — split on periods.

Except `Dr.` is not a sentence. Neither is `St.`, `p.m.`, or `J. R. R. Tolkien`. My first splitter chopped "He lived on Wick St. He kept a ledger" into two fragments, and the narrator dutifully paused in the middle.

Then a subtler one. My sentence splitter matched the punctuation *plus any closing quote* — and consumed both. Every closing quotation mark in the book silently vanished. The audio didn't sound broken; it just lost the shape of dialogue.

And the one I'm still slightly annoyed by. EPUBs inherit line wrapping from print layout, so a paragraph arrives as:

```
In 1885, Dr. Ashworth-Vane kept a ledger of every lamp
on Wick Street. He recorded each one at 6 p.m., and
again at 4:30 a.m.
```

Those newlines mean nothing — they're an artifact of where the page ended. But the synthesiser treats a newline as a breath. My first version narrated that paragraph with a pause after "lamp," after "and," after every ragged line ending. It sounded like someone reading while out of breath.

None of these produce an error. They produce a file that plays. You find them by listening.

## Six hours, not one

I estimated the full book would take about an hour to render. It took six.

The mistake is embarrassing in hindsight: I benchmarked on a two-chapter test file, where most of the elapsed time was loading the model, not generating audio. I extrapolated from a measurement dominated by a fixed cost. Classic.

The real number, measured properly on a machine with no GPU: rendering runs at roughly **1.15× the length of the finished audiobook**. A five-hour book takes about six hours to produce. That's now printed by the tool before you start, because an estimate that's wrong by 6× is worse than no estimate at all.

Which makes the next design decision the load-bearing one.

## If it takes six hours, it has to survive being interrupted

You cannot ask someone to leave a laptop untouched for six hours. So every unit of work — each chapter's text preparation, each individual chunk of speech — is a row in a SQLite database with its own status. Audio files are written under a temporary name and renamed into place, so a half-written file is never marked as done.

Kill the process at any point and you lose at most one chunk. A few seconds of audio.

The part I'm happiest with: each chunk is keyed by a hash of its text *plus every setting that affects the sound*. That one detail buys three behaviours for free. Re-running after an interruption reuses everything already rendered. Changing the narrator's voice correctly re-renders the whole book, instead of splicing two different voices together. And editing the source re-renders only the chapters whose text actually changed.

I got to test this the hard way. Halfway through the real conversion, the source EPUB was deleted off the disk. The tool refused to continue — correctly, but uselessly, since the database already held every chapter's text and hundreds of finished audio chunks were sitting right there. Now a job keeps its own copy of the book and can finish from saved state. The lesson generalises: **a long-running job shouldn't depend on a file it already has the contents of.**

## Two workers made it 2.2× faster. Then they made it slower.

Speech generation on a CPU doesn't saturate a modern machine — the underlying threading tops out around two or three cores, leaving the rest idle for hours. So I split synthesis across several worker processes.

Measured: two workers went from 2.8 to 6.5 chunks per minute. A genuine 2.2× speedup.

Then, an hour later, throughput collapsed to 0.9 chunks per minute. No error. No failed work. Just slow.

The cause was memory. Each worker holds its own copy of the model, around 1.5 GB. When free memory ran low — an editor and a browser had opened in the meantime — the OS trimmed the workers' memory and they started paging. Two workers fighting over RAM is dramatically slower than one worker that fits.

Two things worth taking from that. First, **RAM was the binding constraint, not CPU**, which is the opposite of what "parallelise it" intuition suggests. Second, and more interesting: my sizing logic read *free* memory and was far too pessimistic, because on every OS most of what looks "in use" is reclaimable cache. But even after fixing that, the decision was made once at startup — and the conditions changed underneath it. Some things you cannot solve with a better heuristic. You can only notice and say so, which is what it does now.

## The failure I caused myself

At one point the whole thing appeared to fail silently. Started the server, nothing happened, no output, no error.

I had set the web server's log level to "warning" to keep the terminal tidy. That suppresses *everything* — no startup message, no request logs, no tracebacks. On top of that, conversions run as separate processes writing to their own log file, so a failed render never reached the terminal either.

I had optimised for a clean-looking terminal and, in doing so, made the system undebuggable.

Fixing it surfaced two more bugs within minutes. Uploading a file that was technically a zip but not a valid EPUB returned a 500 error and left an orphaned directory behind. And a freshly uploaded book returned 404 from its own status endpoint, because the database isn't created until conversion *starts* — so the progress bar had nothing to show.

Both had been there the whole time. Neither was findable while the logs were off.

## What it looks like now

One button. Drop in an EPUB, pick a narrator, press **Convert the whole book**. Progress bars fill per chapter. At the end you get a single `.m4b` with chapter markers, cover art, and the metadata tag that makes Apple Books shelve it as an audiobook rather than as music.

The last piece of feedback I got was the most grounding. The finished folder was leaving behind every intermediate audio file: **9.7 MB of scaffolding around a 0.8 MB output**. On a five-hour book that's about 2 GB of debris. "I want one file," was the note. Fair. It now cleans up after itself — though only after a *successful* export, so a failed final step still resumes in seconds instead of re-rendering hours of speech.

## The part that surprised me

I expected the machine learning to be the hard part. It wasn't. The speech model is one dependency and about forty lines of wrapper.

The work was everywhere else: knowing where to cut a sentence, surviving a six-hour runtime, sizing a process pool against memory rather than cores, and being able to see what went wrong. Which is, I suspect, what most software actually is — a thin layer of the interesting thing wrapped in a thick layer of reality.

Every bug I've described here was found by running the thing on a real book and listening to the output. Not one of them was found by the 132 tests. The tests are what keep them fixed.

---

*The code is open source (MIT) at [github.com/shantam21/epub2audiobook](https://github.com/shantam21/epub2audiobook). It runs on Windows, macOS and Linux, costs nothing, and never sends your books anywhere.*
