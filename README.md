# Claude Code ↔ SillyTavern Bridge

**A Flask-based roleplay bridge that wraps the Claude Code CLI as a SillyTavern-compatible backend.**

Sits between SillyTavern and the Claude Code CLI, translating OpenAI-compatible API requests into `claude -p` subprocess calls, injecting narrative-focused system prompts, and layering a full roleplay feature stack on top — per-character running summaries, auto-lorebook generation, image handling via Claude Code's `Read` tool, editable prompt templates, and a GUI dashboard to configure everything.

Uses your **Claude Code subscription**. No API keys, no per-token billing, no credential impersonation. The actual `claude` CLI does the work; the bridge is just a polite translator with features.

![Bridge GUI](docs/screenshot-gui.png)

---

## Why this exists

SillyTavern is an excellent frontend for creative writing and roleplay, but it speaks OpenAI's API format. Claude Code CLI is how you access Claude's best models on a subscription plan — but it's designed for coding, not long-form fiction. This bridge makes them talk to each other, and adds the things you actually want for long RPs that coding assistants don't care about:

- **Per-character running summaries** so 200-message conversations don't re-send the whole backlog every turn
- **Narrative-focused system prompt injection** that overrides Claude Code's built-in "you are a coding assistant" framing
- **Image handling** via Claude Code's native `Read` tool — share reference images in SillyTavern and Claude actually sees them
- **Auto-lorebook** generation that builds World Info entries from your roleplay in the background
- **Live-editable prompts** in the `prompts/` directory — tune summarization and condensation behavior without touching Python

## Features

- OpenAI-compatible `/v1/chat/completions` endpoint (SillyTavern just points at it)
- GUI dashboard at `http://localhost:5001/` with six tabs covering every setting
- **Per-character auto-summary** — each character's narrative digest lives in its own cache slot, keyed by a hash of the greeting. Switching characters auto-swaps summaries; no manual cache clearing.
- **Chunking mode** — one-shot reset that rebuilds a character's summary from an imported chat file
- **Editable prompt templates** at `prompts/*.md` for summarization, condensation, and chunk processing. Hot-reloads on every request — no server restart.
- **Per-character image pipeline** — SillyTavern base64 images get saved to `temp_images/` and injected as file paths so Claude Code's `Read` tool can view them directly
- **Auto-lorebook** generation after each response using Sonnet for efficiency, plus a Deep Analysis mode that scans a full chat file
- **Creativity modes** (Precise / Balanced / Creative / Wild) — prompt-based style control since Claude Code CLI doesn't expose temperature
- **Simulated streaming** with configurable pacing (Off / Slow / Natural / Fast) — Claude Code doesn't emit token deltas, so the bridge paces the completed response through SSE to make SillyTavern render it progressively
- **Settings persistence** — model, effort, creativity, thresholds, and port all survive bridge restarts via `bridge_settings.json`
- **Configurable port** from the GUI (default `:5001`)
- **Tool calling fallback** for SillyTavern extensions like TunnelVision
- **Debug logging** with structured output, token usage panels, and streaming timing diagnostics

## What it actually produces

The GUI shot above is what the tool looks like. This is what it *does* — a mid-RP scene rendered in SillyTavern, pulled from a **142-message roleplay** with the bridge running Opus at high effort, auto-summary active, and the default system prompt:

![Narrative output in SillyTavern](docs/screenshot-narrative.png)

A few things worth noticing that come directly from the bridge's systems:

- **HTML embeds render inline.** The "Guild of Bounties & Bonds" card mid-scene is a rendered block, not escaped text. The default system prompt's `[Tools]` section tells Claude it can use colored dialogs, CSS blocks, and HTML visual elements — and the bridge passes them through without stripping, so SillyTavern renders them live.
- **Color-coded inline spans.** "Red for high threat. Amber for moderate. Green for low." isn't plain text — those are styled spans the model emitted and the pipeline preserved end-to-end.
- **Memory across 100+ turns.** The troll-ear callback ("you left a troll ear on somebody's paperwork") references an event that happened dozens of turns earlier — long enough to have fallen out of any context window without memory management. It survives because the per-character auto-summary preserves *specific details* (the troll ear, the paperwork, the sardonic framing) instead of flattening events into generic recaps. At 142 messages deep, this is exactly the failure mode the summary system is built to prevent.
- **Character integrity holds.** Physical tics, speech patterns, decision-making — Grimya stays Grimya across the whole scene and across the whole chat. The system prompt's character-integrity rules are doing real work here.
- **No slop.** Specific tactile detail, concrete observations, earned dialogue beats. That's the combination of the creativity-mode prompt + the system prompt's explicit rules against soft hedge-writing.

This is what the bridge is *for*. The features in the list above all exist to make scenes like this one possible and consistent across long RPs.

## Requirements

- **Python 3.10+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** installed and authenticated (run `claude` once interactively to confirm it works)
- **Claude subscription** (Pro, Max, or equivalent) with Claude Code access
- **SillyTavern** installation (for the actual UI; the bridge is backend-only)

## Install

```bash
git clone https://github.com/MissSinful/claude-code-sillytavern-bridge.git
cd claude-code-sillytavern-bridge
pip install -r requirements.txt
```

Start the bridge:

**Windows:**
```cmd
run_bridge.bat
```

**macOS / Linux:**
```bash
python claude_bridge.py
```

The bridge starts on `http://localhost:5001`. Open it in a browser to see the dashboard.

## SillyTavern setup

1. In SillyTavern, open **API Connections**
2. Select **Chat Completion** → **OpenAI Compatible** (or "Custom OpenAI" depending on your ST version)
3. Set the endpoint to `http://localhost:5001/v1`
4. Enter any API key — the bridge doesn't check it, but SillyTavern requires the field to be non-empty. `sk-placeholder` works.
5. Model selector in SillyTavern is ignored — pick your model in the Settings tab of the bridge GUI instead
6. Save and connect

Send a test message. If it works, you're set. If not, check the bridge terminal for logs — debug output is on by default.

## Using it

Most features work automatically once the bridge is running and configured. Highlights:

**Enable auto-summary** early in a new chat. Tools tab → toggle Auto-Summary on. The default threshold updates the summary every 20 new messages, which is a reasonable balance for Opus-effort-high usage caps. Without it, SillyTavern re-sends your entire message history on every turn, which eats through usage limits fast on long RPs.

**Editing prompts.** The bridge's internal prompts live as markdown files in `prompts/`:

| File | What it does |
|---|---|
| `summarize_incremental.md` | Appended each time auto-summary updates |
| `condense.md` | Condenses a summary when it grows past the max-length threshold |
| `summarize_chunk.md` | Per-chunk summary for both live chunking and file-based chunking |
| `condense_chronological.md` | Final chronological condensation for file-based summary generation |

Edit any of these, save, and the next request picks up the change. No server restart. Placeholders use Python `{variable}` syntax — escape a literal brace as `{{` / `}}`.

**The main RP system prompt** is *not* in the `prompts/` folder — it's the `DEFAULT_BRIDGE_SYSTEM_PROMPT` constant in `claude_bridge.py`. Edit it via the System Prompt tab in the GUI, which persists to `bridge_settings.json`.

## Known limitations

These are **architectural**, not bugs — they're properties of running Claude Code CLI as a subprocess per request, and there's no clean fix inside the current CLI version.

- **No real token streaming.** Claude Code CLI doesn't emit incremental `content_block_delta` events for subprocess callers — it ships the full response in one `assistant` event at the end. The bridge collects the full response and then paces it through SSE (configurable via Simulated Streaming) so the SillyTavern experience still feels streamed. Total wall-clock time is `model_time + stream_pace_time`.
- **No temperature / sampling parameters.** Claude Code CLI doesn't expose `temperature`, `top_p`, or `top_k`. The Creativity setting (Precise / Balanced / Creative / Wild) is a prompt-based style modifier — Claude follows the style instructions, but it's not the same as a real temperature slider.
- **Per-request subprocess overhead.** Every RP turn spawns a fresh `claude -p` process, which adds startup latency compared to a persistent HTTP client. Not a problem for the long thinking times typical of high-effort requests, but noticeable for small ones.
- **No prompt caching control.** Claude Code manages its own caching internally; the bridge can't directly set cache breakpoints. Auto-summary mitigates this by keeping the stable prefix large and the variable suffix small.

If any of these become dealbreakers for you, the right move is an alternative backend mode that hits the Anthropic SDK directly — the groundwork is there, but it's not currently implemented.

## Project structure

```
claude-code-sillytavern-bridge/
├── claude_bridge.py           # Main Flask server and subprocess wrapper
├── modify_preset.py           # Standalone utility for SillyTavern preset tweaks
├── requirements.txt           # Python dependencies
├── run_bridge.bat             # Windows launcher
├── templates/
│   └── index.html             # GUI dashboard (single-page, vanilla JS)
├── prompts/                   # Editable prompt templates (hot-reloaded)
│   ├── summarize_incremental.md
│   ├── condense.md
│   ├── summarize_chunk.md
│   └── condense_chronological.md
├── cache/                     # Per-character summary cache (gitignored)
├── temp_images/               # SillyTavern base64 image dumps (gitignored)
├── bridge_settings.json       # Persisted runtime settings (gitignored)
└── CLAUDE.md                  # Project notes for Claude Code itself
```

## Content note

The default system prompt framing is for **adult collaborative fiction**. It includes explicit instructions for how to handle intimate scenes authentically rather than theatrically, alongside instructions for character integrity, narrative risk-taking, and structured thinking. This is intentional — the bridge is built for adult RP and storytelling, and the prompt is what makes Claude not default to sanitized boilerplate when the story calls for more.

If that doesn't match your use case, replace `DEFAULT_BRIDGE_SYSTEM_PROMPT` in `claude_bridge.py` with your own framing, or override it via the System Prompt tab in the GUI.

## Policy & responsibility

This tool doesn't modify or bypass Anthropic's safety systems — it pipes your prompts to the official `claude` CLI using your own subscription via its intended auth flow. Anything Claude would refuse in `claude.ai` it'll still refuse here; the default system prompt steers tone and framing but can't (and doesn't try to) override model-level safety training. Content you generate through this bridge is subject to [Anthropic's Acceptable Use Policy](https://www.anthropic.com/aup), and responsibility for what you prompt and what the model produces stays with you as the subscriber.

## Maintenance

**Personal project shared for anyone who finds it useful.** I built this for my own SillyTavern + Claude Code workflow and polished it enough to be worth putting up. The architecture is stable, the features work, and the known limitations are documented above. PRs welcome; issues may or may not get responses depending on how relevant they are to my own usage.

If you want to take any of the architectural gaps (real streaming via the Anthropic SDK, proper temperature control, prompt caching) and turn them into a PR, I'll review it. But don't expect a responsive upstream. This is closer to "published for reference" than "actively maintained."

## License

MIT. See `LICENSE`.
