# Output style for the embedded chat

The embedded widget renders whatever markdown a sub-agent emits, in a panel about
**400px wide** (≈370px of usable content). CSS can only do so much: a three-column
table of long sentences is unreadable at that width no matter how it is styled. So
output shaping is split in two:

- **This renderer's job** — never mangle what arrives. Wide tables scroll, values stay
  on one line, prose wraps at a readable measure, code blocks scroll.
- **The sub-agent's job** — prefer shapes that fit a narrow panel in the first place.

Sub-agent system prompts are **not in this repo**: they live in
`sub_agent_config_versions.system_prompt` and are edited per sub-agent in the Console.
Paste the block below into the relevant sub-agent's prompt (adjust the domain wording).

## Paste into a sub-agent's system prompt

```text
## Output formatting

Your answers render as markdown in a narrow (≈370px) embedded panel. Format for it:

- Lead with a short bolded verdict line, then details. No preamble.
- Prefer a TWO-COLUMN key/value table for figures. Leave the header row empty
  (`|  |  |`) — the UI hides blank headers and renders it as a metrics block with
  the values right-aligned:

  |  |  |
  | --- | --- |
  | Total budget | CHF 195,000 |
  | Spent to date | CHF 187,320 (96%) |

- Use three or more columns only for genuinely tabular comparisons, and keep each
  cell under ~40 characters. Wide tables scroll sideways, so anything past the third
  column is easy to miss — put the important column first.
- Never put a sentence in a table cell. Move explanations to a bullet under the table.
- One status emoji at the start of a line is fine (🔴 🟡 ✅ ⚠️). Do not build a column
  of emoji, and never rely on emoji alone to carry meaning — always name the state.
- Keep paragraphs to one or two sentences; the panel is tall and thin.
- Use `inline code` for identifiers (ids, field names, tool names), and fenced blocks
  only for payloads worth copying.
- No H1. Start at `###` if you need a heading at all.
```

## What the renderer guarantees

| Markdown the agent emits | How it renders |
| --- | --- |
| 2-column table with a blank header | Key/value block, header hidden, values right-aligned in monospace |
| 2-column table with a real header | Same block, header shown |
| 3+ column table | Sizes to content and scrolls horizontally inside its own container |
| Numeric-looking cell (`96%`, `CHF 187,320`, `4.2 M`) | Right-aligned, monospace, never wrapped |
| Cell under 48 characters | Stays on one line |
| Cell over 48 characters | Wraps at ~20rem, not at the column width |
| Fenced code block | Dark block, scrolls horizontally, never widens the panel |
| Long unbroken token (URL, id) | Breaks rather than stretching the message card |

Implementation: `packages/embed-sdk/src/components/ui/markdown.tsx`. The dev harness
(`npm run dev` in `packages/embed-sdk`) renders these cases as fixtures — add a fixture
there when you hit a new shape that renders badly, so the fix can be seen without a
backend.

## Note on richer widgets

Anything beyond markdown (interactive cards, charts, forms) is a different mechanism:
the agent emits a client action or a structured part and the host renders it. Prompt
wording cannot conjure a widget the UI has no component for — extend the UI first, then
instruct the sub-agent to target it.
