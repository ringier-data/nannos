# Console Frontend Copilot Instructions

## Maintaining These Instructions

When implementing new features or refactoring existing code, consider if these instructions need updating. Only document design decisions that are non-obvious and would require reading large portions of the codebase to understand them.

## Tech Stack

- React with TypeScript
- Vite as the build tool
- React Router for routing
- Tailwind CSS for styling
- shadcn/ui for UI components

## Local Development Environment

**CRITICAL: Any changes that impact the local development environment MUST be reflected in `/start-dev.sh`**

This includes:
- New environment variables (add to .env generation in start-dev.sh)
- Configuration changes that affect local setup
- New service dependencies or startup requirements
- Build configuration changes that affect how the app runs locally

The `start-dev.sh` script is the single source of truth for local environment setup. Always update it when making changes that affect how the application runs locally.

## Code Style

- Use functional components with hooks
- Use the `@/` alias for imports from `src/`
- Prefer named exports over default exports

## File Writing Safety

NEVER use heredoc (`cat << EOF`) to write files - causes fatal errors. Use incremental edits with proper file writing tools instead.

## Components
- Do not always use cards for everything. Use them judiciously based on context.
- Follow the existing design patterns and component structures in the codebase.

# Backend Integration
- The backend API is defined using OpenAPI specifications.
- The SDK for API calls is auto-generated using the OpenAPI Generator.
- To regenerate the SDK after backend API changes, run the following command in the terminal:
```
npm run gen-sdk
```
- Regenerate the SDK every time there are changes to the backend API.


## A2A Extension Event Handling

The frontend receives A2A events via Socket.IO from the agent-console. Events carry extension markers that determine how they're classified and displayed.

### Extension Constants (types.ts)

```typescript
const ACTIVITY_LOG_EXT = 'urn:nannos:a2a:activity-log:1.0';
const WORK_PLAN_EXT = 'urn:nannos:a2a:work-plan:1.0';
const INTERMEDIATE_OUTPUT_EXT = 'urn:nannos:a2a:intermediate-output:1.0';
```

### Event Classification in ChatContext.tsx

| Event kind | Extension check | Behavior |
|------------|----------------|----------|
| `status-update` | `message.extensions` includes `WORK_PLAN_EXT` | Extract `DataPart.data.todos`, merge by source into `workingStepsMap`, display in sticky `WorkingBlock` |
| `status-update` | `message.extensions` includes `ACTIVITY_LOG_EXT` | Extract text, push to `statusHistoryMap`, display in timeline — NOT as message bubble |
| `artifact-update` | `artifact.extensions` includes `INTERMEDIATE_OUTPUT_EXT` | Accumulate chunks by `agent_name` in `subagentThoughtsMap`, display as thinking blocks |
| `artifact-update` | No intermediate-output extension | Accumulate into `streamingMap` as the main response text |
| `status-update` | `state === 'completed'` | Finalize streamed text, build timeline, clear transient state |

### Sticky Working Block

- `liveWorkingSteps` drives a sticky `WorkingBlock` widget between the scroll area and chat input
- The `complete` prop is derived from `!isWaiting` (shows spinner during streaming, checkmark after)
- Working steps are NOT deleted on task completion — they persist until the next user message
- Todos are NOT included in the timeline (removed from `buildTimeline()`)

### Timeline Building

`buildTimeline(thoughts, history, timestamp)` produces a chronological `TimelineEvent[]` from:
- `statusHistoryMap` → `{type: 'status'}` events
- `subagentThoughtsMap` → `{type: 'thought_start/thought_end'}` events

Timeline events are attached to finalized messages and reconstructed from `raw_payload` when loading history.

## API Calls

- Use the generated SDK under `src/api/` for all backend API calls
- Do NOT use `fetch` or `axios` directly for backend calls
- If the SDK is outdated or missing endpoints, ask the user to regenerate it by running:
  ```bash
  npm run gen-sdk
  ```
