---
name: create-a-sub-agent
description: Design and create a sub-agent the orchestrator will actually route to — pick the type, write a routing-quality description and a focused system prompt, choose MCP tools by discovery, a model tier, skills, and explain whether it needs approval — then fill the form on screen or call console_create_sub_agent. Use when the user wants a new specialist agent or to rework one — "make me an agent that reviews contracts", "create a sub-agent for our Jira triage", "why does the orchestrator never use my agent?".
---

# Create a sub-agent

Goal: a sub-agent with a description the orchestrator can route on, the tools and
model it needs and nothing more, saved through the form when one is open.

Steps:

1. Understand the job: which requests it should take, which data and systems it
   touches, what a good answer looks like. Check console_list_sub_agents first —
   an existing agent (or a copy of the idea) may already cover it.
2. Type: local unless the user already runs the agent elsewhere (remote, needs its
   A2A URL) or it is a Foundry query (foundry, needs hostname, client id, a client
   secret in the Secrets Vault, ontology RID, query API name, scopes).
3. Name: an identifier like "contract-reviewer" (letter first; letters, digits,
   "-", "_"; no spaces).
4. Description, written for the orchestrator: "Handles X for Y using Z. Use for …;
   not for …". Name the concrete task types and data sources. This decides whether
   the agent is ever picked.
5. System prompt (local): role, the steps it should follow, output format, and what
   to refuse or hand back. Keep it tight — see approval below.
6. Tools (local): console_grep_mcp_tools for each capability the job needs; choose
   the specific tools, use names exactly as returned. An empty list means no MCP
   tools at all.
7. Model: a tier — standard by default, low for simple high-volume work, premium
   for hard reasoning. A concrete model only if the user asks (console_list_models;
   extended thinking needs a concrete model that supports it).
8. Skills: console_search_skills for reusable procedures. Activate others' skills
   pinned unless the user explicitly wants them following. Write a new one with the
   write-a-skill skill when the procedure is specific to this agent.
9. Access: private by default; public only if the user wants every user to have it.
   Sharing with specific groups happens on the agent's page.
10. Save:
    - Create form open (/app/subagents/new) or the agent's edit form → apply: type
      first, then name, description, model, system prompt, tools, thinking. Then
      invoke `save` when the user wants it saved; on the edit form that opens the change
      summary dialog — fill its summary with apply and invoke that dialog's `save`.
    - No form open → summarise and, on confirmation, console_create_sub_agent
      (type, name, description, model_tier or model, system_prompt, mcp_tools,
      skills). For later changes: console_update_sub_agent — but never while that
      agent is open in a form.
11. Tell the user what happens next: it must be activated in Settings → Sub-Agents
    to be used by their orchestrator.

Approval:

- Approved automatically: local, private, system prompt (plus inlined skills) at
  most 500 characters, at most 3 MCP tools (server defaults).
- Otherwise it is created as a draft: the owner submits it for approval with a
  change summary, and an approver approves or rejects it. Say this up front when the
  design exceeds the limits, and offer the leaner variant if it would do the job.
- Every later save is a new version; people keep running the approved default
  version until a new one is approved.

Edge cases:

- "The orchestrator never uses my agent" → read its description first; it is almost
  always too vague or overlaps another agent. Then check it is activated and approved.
- Embedded (host-bound) agent → prompt, skills and published fields are read-only
  here; changes go to the host's repository.
- The user asks for "all tools" → explain the trade-off and pick the tools the job
  needs instead.
