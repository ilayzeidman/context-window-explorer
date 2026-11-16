# context-window-explorer

The [context window page](https://code.claude.com/docs/en/context-window) in the Claude Code docs has a simulated
timeline of what enters the context. This tool draws the same picture from your real session files.

At any point in a session it shows two things side by side:

- **On disk**: every record Claude Code wrote to the session file so far.
- **In the window**: what the model reads on the next request (the `parentUuid` chain back to the last compaction).

## Run

```
python ctxview.py                 # newest session of the current folder
python ctxview.py 2632af7a        # by session id prefix, searched across ~/.claude/projects
python ctxview.py path/to/x.jsonl  # its folder becomes readable too
```

Python 3.9+, standard library only. Opens `http://127.0.0.1:8765`. Options: `--port N`, `--at LINE`, `--no-open`.

## What you can do

- Scrub the timeline. Use the arrow keys to step one record and `[` `]` to jump between events.
- Link to a moment: `?path=<file>&at=<line>&mark=<text>` opens that record with its text showing and the text highlighted (`||` separates several). `--at LINE` does the same from the command line.
- Click a marker to get a card explaining it: nested CLAUDE.md, a big tool output saved to disk, a subagent, a
  compaction (before and after), `/clear`, `/model`, a skill call, or a gap that let the cache go cold.
- See a record raw: `R` (or `&raw=1`) shows open rows as the JSON line on disk, colored, with long strings folded. The fields
  repeated on every record are greyed out.
- Open a subagent's own file in the same view.
- **Cache per request**: cache read, cache write and uncached input for every request.
- **Live tail**: follow a session while Claude Code writes it.

## How the numbers are made

- The total at the top comes from the API's own count for the next request (`input + cache read + cache write`).
- Per-record sizes are estimates at 4 characters per token. The bar shows the difference to the API count as two
  grey parts:
  - **System prompt + tools**: the gap on the first request, when there are almost no messages yet. A heuristic.
  - **Unattributed**: the rest of the difference, which grows with the window. Likely causes are code and JSON
    tokenizing denser than 4 characters, images, thinking blocks stored empty, and tool schemas loaded mid-session.
    Not measured per cause.
- After a compaction, the kept-verbatim tail is stored before the boundary. The viewer splices it back in after the
  summary (`compactMetadata.preservedSegment`).

## The request as sent

Claude Code 2.1.325 and later also writes a copy of every API request into the session file: a **shape** (system
prompt + tools, written once and reused), a **blob** per new message, and a **request** record that lists which
messages go out, in what order, with which cache marks. When a file has them:

- `A` switches the middle column between "In the window" (the chain above) and "As sent": system blocks, tools, then
  each message with its role and blocks, sizes and cache marks. Click a block for its full text.
- The bar is drawn from that request, so there is no grey gap: system prompt, tools and tool additions get their own parts.
- Only the total is measured (the API's count on the response). The parts are apportioned by size: the request's
  characters divided by that total gives a chars-per-token rate for this request (images count 1,600 tokens each),
  and each part gets its characters at that rate, rounded so the parts sum to the total. Before the response
  arrives, the previous request's rate is used.

Older files have none of these records and look exactly as before.

## Privacy

Session files hold your full chats and anything secret that passed through them. The server listens on 127.0.0.1
only. It reads only files under `~/.claude/projects` (set `CLAUDE_PROJECTS_DIR` to change that) and sends nothing
anywhere. Rows show a one-line preview; the full text loads only when you click a row. Previews and session titles
are still visible, so pick the session before you share your screen.

## Test

```
python test_ctxview.py
```

It runs against `fixtures/`, a small synthetic session.
