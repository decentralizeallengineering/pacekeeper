# Captured Claude Code 2.1.311 payloads

These files were captured from a real Claude Code 2.1.311 run on Linux on
2026-10-27, then scrubbed. They are not hand-written.

| File | What it is |
|---|---|
| `statusline-before-first-reply.json` | stdin of the status line command on its first refresh, before any API reply: no `rate_limits` key |
| `statusline.json` | stdin of the status line command after the first reply, with `rate_limits.five_hour` and `seven_day` |
| `hook-UserPromptSubmit.json` | stdin of a `UserPromptSubmit` hook command |
| `hook-PostToolUse.json` | stdin of a `PostToolUse` hook command (after a Bash tool call) |
| `transcript.jsonl` | the user, assistant and hook lines of a transcript in which `pacekeeper hook` injected a note on both events |

Scrubbed: session ids, prompt ids, message, request and tool-use ids, every
path (now under `/home/ann/demo`), the model id and display name (now
`model-a` / `Model A`), thinking text and signatures, and the hook command
path. Kept as captured: every key, the structure, the `rate_limits` numbers,
`resets_at` (epoch seconds), token counts, timestamps and the version string.

How they were captured: a status line command and both hook commands that
copied their stdin to a file (`tee`), in an interactive `claude` session for
the status line and in `claude -p` for the hooks. The transcript shows the
hook output arriving in the model's context as
`<system-reminder>UserPromptSubmit hook additional context: ...` and
`PostToolUse:Bash hook additional context: ...`, and the model quoting it.
