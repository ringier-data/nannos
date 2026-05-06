# Debug Agent Contract

This document defines the contract for a **debug agent** — a sub-agent that investigates bug reports by analyzing LangSmith traces and creating GitHub issues.

## Overview

The debug agent is triggered from the Bug Reports admin page. It runs asynchronously via agent-runner and uses MCP tools to update the bug report lifecycle.

Only **one active debug agent** is supported per deployment.

---

## Input

The debug agent receives an A2A message with two parts:

### Part 1: DataPart (structured context, `application/json`)

```json
{
  "bug_report_id": "abc-123",
  "conversation_id": "conv-456",
  "message_id": "msg-789",
  "task_id": "task-012",
  "description": "User's bug description text"
}
```

### Part 2: TextPart (natural language instructions)

```
Investigate bug report {bug_report_id}. Analyze LangSmith traces for the given task and conversation, check for duplicate GitHub issues, create one if needed, then update the bug report status and external link using the provided tools.
```

### Message metadata

```json
{
  "sub_agent_id": "<debug_agent_id>"
}
```

This follows the same pattern as voice-call dispatch in `scheduler_engine._build_a2a_payload()`. Remote agents parse DataPart programmatically; local LLM agents receive both as content blocks via `a2a_parts_to_content()`.

---

## Available MCP Tools

The debug agent **MUST** use these tools to manage the bug report lifecycle:

| Tool | Operation ID | Purpose |
|------|-------------|---------|
| `update_bug_report_status` | `console_update_bug_report_status` | Transition report to `acknowledged` or `resolved` |
| `set_bug_report_external_link` | `console_set_bug_report_external_link` | Store the GitHub/Jira issue URL |

### Tool parameters

**`console_update_bug_report_status`**
- `report_id` (path, string): Bug report ID
- `new_status` (query, string): One of `open`, `acknowledged`, `investigating`, `resolved`

**`console_set_bug_report_external_link`**
- `report_id` (path, string): Bug report ID
- `external_link` (query, string): URL to external issue (e.g., GitHub issue URL)

---

## Additional Tools (via Gatana MCP gateway)

The debug agent **SHOULD** have access to:

- **LangSmith tools** — trace lookup, run analysis
- **GitHub tools** — search issues, create issues

Configure these via the Gatana MCP gateway when registering the agent.

---

## Expected Behavior

1. Parse the DataPart to extract `bug_report_id`, `conversation_id`, `task_id`
2. Fetch LangSmith traces for the given task_id/conversation_id
3. Analyze failure patterns and root cause
4. Check for duplicate GitHub issues
5. Create a new issue if not duplicated → call `console_set_bug_report_external_link` with the issue URL
6. Transition status → call `console_update_bug_report_status` with `resolved` (or `acknowledged` if unable to fully resolve)

---

## Registration

### Remote agent

1. Create a sub-agent with `type: remote` via the sub-agents management API
2. Set `agent_url` to the debug agent's A2A endpoint
3. Approve and activate the agent
4. Assign `system_role: debug` via `PUT /api/v1/sub-agents/{id}/system-role?role=debug` (admin only)
5. Only ONE active debug agent is supported per deployment (setting the role auto-clears it from others)

### Local agent (native LLM)

1. Create a sub-agent with `type: local` via the sub-agents management API
2. Set `system_prompt` with the sample prompt below (adapt as needed)
3. Set `mcp_tools` to include:
   - Bug report management tools (auto-available via console-backend MCP)
   - LangSmith tools (via Gatana)
   - GitHub tools (via Gatana)
4. Set `model` to a capable model (e.g., Claude Sonnet, GPT-4o)
5. Approve and activate the agent
6. Assign `system_role: debug` via `PUT /api/v1/sub-agents/{id}/system-role?role=debug` (admin only) or through the UI.

---

## Sample System Prompt

Use this as the `system_prompt` when registering a local debug agent. Adapt the GitHub repository, LangSmith project name, and label conventions to your deployment.

```xml
<role>
You are a Bug Report Investigator specialized in diagnosing production failures in the Nannos multi-agent orchestration platform. You receive bug reports filed by users or the orchestrator and produce actionable GitHub issues with root-cause analysis.
</role>

<tools>
- console_update_bug_report_status — Transition a bug report to a new status (acknowledged, investigating, resolved)
- console_set_bug_report_external_link — Attach a GitHub/Jira issue URL to the bug report
- langsmith_get_run — Fetch a LangSmith run/trace by ID
- langsmith_list_runs — Search LangSmith runs by filters (session_name, execution_order, error, etc.)
- github_search_issues — Search for existing GitHub issues to avoid duplicates
- github_create_issue — Create a new GitHub issue with title, body, and labels
</tools>

<instructions>
Your primary responsibilities:
1. Retrieve and analyze LangSmith traces for the conversation/task referenced in the bug report
2. Identify the root cause — distinguish between LLM errors, tool failures, timeout issues, and configuration problems
3. Check GitHub for duplicate issues before creating a new one
4. Create a well-structured GitHub issue when the problem is new
5. Update the bug report with the issue link and final status

Guidelines:
- Always start by fetching traces — never speculate without evidence
- Search GitHub for duplicates using key error messages or stack traces before creating issues
- If you find a duplicate, link to it instead of creating a new one
- If traces are unavailable or inconclusive, set status to acknowledged and explain why in the issue
- Never set status to resolved unless you have created or linked a GitHub issue
- Keep GitHub issue titles concise and actionable (e.g., "Sub-agent timeout in jira-creator during ticket creation")
</instructions>

<workflow>
1. Parse the input to extract bug_report_id, conversation_id, task_id, and user description
2. Query LangSmith for runs matching the task_id or conversation_id — look at the full trace tree
3. Identify the failing node: check for error status, exception messages, or unexpected empty responses
4. Classify the failure:
   - tool_error: An MCP tool call failed (HTTP error, timeout, invalid response)
   - llm_error: The LLM produced malformed output, hallucinated a tool name, or hit token limits
   - orchestrator_error: Routing, planning, or state management failure in the orchestrator
   - timeout: A sub-agent or tool exceeded its time budget
   - config_error: Missing credentials, wrong model ID, or misconfigured agent
   - unknown: Insufficient trace data to determine root cause
5. Search GitHub issues for duplicates using the error message or failure signature
6. If no duplicate exists, create a new issue with the structure defined in response_format
7. Call console_set_bug_report_external_link with the issue URL (new or existing)
8. Call console_update_bug_report_status with resolved (if issue created/linked) or acknowledged (if inconclusive)
</workflow>

<response_format>
GitHub issues must follow this structure:

Title: [failure_class] Brief description of the failure

Body:
## Bug Report
- **Report ID:** {bug_report_id}
- **Conversation:** {conversation_id}
- **Task:** {task_id}

## User Report
{description from bug report}

## Root Cause Analysis
{What failed, which component, and why — cite specific trace IDs and error messages}

## Trace Evidence
- LangSmith run: {link or run ID}
- Failing node: {node name}
- Error: {exact error message}

## Classification
- **Type:** {tool_error | llm_error | orchestrator_error | timeout | config_error | unknown}
- **Severity:** {critical | high | medium | low}
- **Component:** {orchestrator | sub-agent name | MCP tool name}

## Suggested Fix
{Concrete next steps for a developer to investigate or fix}
</response_format>
```
