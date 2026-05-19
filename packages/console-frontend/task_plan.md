# Frontend: Skills Registry Integration

## Goal
Add a "Discover & Import" experience to the existing Skills page, enabling users to search the platform registry, browse GitHub repos, preview skills (with security assessment), and import them — all without leaving the Skills page.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  SkillsPage.tsx (existing: /app/skills)                         │
│                                                                 │
│  ┌──────────┐  ┌──────────────────────────────────────────────┐│
│  │ Sidebar  │  │ Main Panel                                    ││
│  │          │  │                                               ││
│  │ Agent ▼  │  │ [All] [Personal] [Group:X] [Standard]        ││
│  │          │  │                                               ││
│  │ Skills   │  │ ┌─ SkillEditorPanel (existing) ──────────┐   ││
│  │ • skill-a│  │ │ SKILL.md editor + bundled files         │   ││
│  │ • skill-b│  │ │                                         │   ││
│  │          │  │ └─────────────────────────────────────────┘   ││
│  │ ─────────│  │                                               ││
│  │ [+ New]  │  │ ── OR ──                                     ││
│  │ [↓ Import│  │                                               ││
│  │          │  │ ┌─ SkillImportPanel (NEW) ─────────────────┐  ││
│  └──────────┘  │ │ Search | Browse | Preview | Import       │  ││
│                │ └──────────────────────────────────────────┘  ││
│                └───────────────────────────────────────────────┘││
└─────────────────────────────────────────────────────────────────┘
```

### UX Flow

```
1. User clicks "Import" button in sidebar
   → SkillImportPanel opens in main panel (replaces editor)

2. Panel shows:
   - Search bar with source toggle (Registry | Community | Browse Repo)
   - Results grid/list with skill cards

3. User clicks a result card
   → Preview slide-over or inline expansion shows:
   - SKILL.md content preview
   - Security verdict badge (safe/caution/unsafe)
   - File list
   - Import button with scope selector (personal/group)

4. User clicks "Import"
   → Calls importSkillApiV1SkillsRegistryImportPost
   → Shows success toast + refreshes skill list
   → Switches to the imported skill in the editor
```

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Where to put the import UX | Same SkillsPage, as a panel state | No extra route; keeps context (agent selector, scope tabs) |
| Import panel vs. modal | Panel (replaces editor area) | More room for search results + preview than a modal |
| Search debounce | 300ms | Avoid hammering API on every keystroke |
| Browse repo input | Freeform `owner/repo` with validation | Git-first; user knows their repos |
| Security badge display | Colored badge + expandable indicators | Users need at-a-glance safety + drill-down detail |
| Agent targeting | Pre-selected from sidebar agent selector | Natural — user already chose which agent they're working with |
| Scope default | "personal" | Fast, no-approval import; promote later if needed |

---

## Phase 1: SkillImportPanel — Search & Results
**Status:** `not_started`

### What to build
New component `src/components/skills/SkillImportPanel.tsx`:
- Search input with debounced query
- Source toggle: "Registry" (default) | "Community" | "Browse Repo"
- Results list using `searchSkillsApiV1SkillsRegistrySearchGetOptions` or `browseRepoApiV1SkillsRegistryBrowseGetOptions`
- Each result card shows: name, source, description (if available), install count badge
- Loading skeleton during fetch
- Empty state for no results

### Integration with SkillsPage
- Add "Import" button to sidebar actions (next to "+ New" button)
- State: `showImport: boolean` — when true, main panel shows SkillImportPanel instead of SkillEditorPanel
- Pass `agentName` and `groupId` as props to SkillImportPanel

### Generated hooks to use
```typescript
import { searchSkillsApiV1SkillsRegistrySearchGetOptions } from '@/api/generated/@tanstack/react-query.gen'
import { browseRepoApiV1SkillsRegistryBrowseGetOptions } from '@/api/generated/@tanstack/react-query.gen'
```

### Files to create/modify
- `src/components/skills/SkillImportPanel.tsx` (new)
- `src/pages/SkillsPage.tsx` (add Import button + panel toggle)

---

## Phase 2: Skill Preview & Security Badge
**Status:** `not_started`

### What to build
When user clicks a search result:
- Expand/slide-over showing full skill detail
- Fetch detail via `getSkillDetailApiV1SkillsRegistryDetailSkillIdGetOptions` (registry) or inline from browse results
- Show SKILL.md content in read-only markdown preview
- Security verdict badge component:
  - **Safe** → green badge (Shield icon)
  - **Caution** → yellow badge (AlertTriangle icon)
  - **Unsafe** → red badge (ShieldAlert icon)
- Expandable indicators list (when verdict is caution/unsafe)
- File list (non-editable)

### New component
`src/components/skills/SecurityVerdictBadge.tsx`:
- Props: `verdict: SkillSecurityVerdict`
- Renders: colored Badge + tooltip with reasoning
- Expandable: shows indicators with evidence snippets

### Files to create/modify
- `src/components/skills/SecurityVerdictBadge.tsx` (new)
- `src/components/skills/SkillImportPanel.tsx` (add preview state)

---

## Phase 3: Import Action
**Status:** `not_started`

### What to build
Import button in the preview that:
- Shows scope selector dropdown (personal | group)
- Calls `importSkillApiV1SkillsRegistryImportPost` with:
  - `repo` + `skill` (from the selected result)
  - `agent` (from current agent selector)
  - `scope` (from scope dropdown)
  - `group_id` (from current group context if scope=group)
- Shows loading state during import
- On success:
  - Toast notification with security verdict summary
  - Invalidates skill list query (auto-refreshes sidebar)
  - Switches to the imported skill in SkillEditorPanel
  - Closes import panel
- On 403 (unsafe):
  - Shows security warning dialog with indicators
  - Option to "Force Import" (if user has approver role)
- On 409 (conflict):
  - Shows "already exists" message with overwrite option

### Generated hooks to use
```typescript
import { importSkillApiV1SkillsRegistryImportPostMutation } from '@/api/generated/@tanstack/react-query.gen'
```

### Files to create/modify
- `src/components/skills/SkillImportPanel.tsx` (add import logic)
- `src/pages/SkillsPage.tsx` (handle post-import state transition)

---

## Phase 4: Browse Repo Mode
**Status:** `not_started`

### What to build
When source toggle is "Browse Repo":
- Show additional input field for `owner/repo` (with format validation)
- Optional ref field (defaults to "main")
- Submit button to trigger browse
- Results come from `browseRepoApiV1SkillsRegistryBrowseGetOptions`
- Same result cards + preview + import flow as registry search

### UX
- Input validation: must be `owner/repo` format (contains exactly one `/`)
- Auto-submit on Enter after validation
- Remember last browsed repo in sessionStorage

### Files to modify
- `src/components/skills/SkillImportPanel.tsx` (add browse mode UI)

---

## Phase 5: Registry Admin Panel (optional)
**Status:** `not_started`

### What to build (stretch goal)
For admins/approvers: a view of imported registry entries with management actions:
- List all registry entries (internal search with no query = list all)
- Per-entry actions:
  - Change visibility (group → public, public → group)
  - Remove from registry
  - View security assessment
- Filterable by visibility, group, source type

### Could be:
- A new tab in SkillsPage ("Registry" tab alongside All/Personal/Group/Standard)
- Or a sub-page under admin routes

### Generated hooks
```typescript
import { removeSkillApiV1SkillsRegistrySkillIdDeleteMutation } from '@/api/generated/@tanstack/react-query.gen'
import { updateVisibilityApiV1SkillsRegistrySkillIdVisibilityPatchMutation } from '@/api/generated/@tanstack/react-query.gen'
import { activateSkillApiV1SkillsRegistrySkillIdActivatePostMutation } from '@/api/generated/@tanstack/react-query.gen'
```

### Files to create/modify
- `src/components/skills/RegistryPanel.tsx` (new) — or integrated into SkillsPage
- `src/pages/SkillsPage.tsx` (add Registry tab if integrated)

---

## Dependencies Between Phases

```
Phase 1 (Search + Results) ──→ Phase 2 (Preview + Security) ──→ Phase 3 (Import Action)
                                                                         │
Phase 4 (Browse Repo) ──────────────────────────────────────────────────┘
                                                                   (uses same preview/import)

Phase 5 (Admin) — independent, optional
```

Phase 1 is the foundation. Phase 2+3 complete the core flow. Phase 4 adds an alternative discovery method.

---

## Available API Infrastructure (already generated)

### React Query Options (GET)
| Hook | Endpoint | Purpose |
|------|----------|---------|
| `searchSkillsApiV1SkillsRegistrySearchGetOptions` | `GET /skills/registry/search` | Search registry or external |
| `browseRepoApiV1SkillsRegistryBrowseGetOptions` | `GET /skills/registry/browse` | Scan GitHub repo |
| `getSkillDetailApiV1SkillsRegistryDetailSkillIdGetOptions` | `GET /skills/registry/detail/{id}` | Full skill detail |

### React Query Mutations (POST/DELETE/PATCH)
| Hook | Endpoint | Purpose |
|------|----------|---------|
| `importSkillApiV1SkillsRegistryImportPostMutation` | `POST /skills/registry/import` | Import + activate |
| `removeSkillApiV1SkillsRegistrySkillIdDeleteMutation` | `DELETE /skills/registry/{id}` | Remove from registry |
| `activateSkillApiV1SkillsRegistrySkillIdActivatePostMutation` | `POST /skills/registry/{id}/activate` | Activate for agent |
| `updateVisibilityApiV1SkillsRegistrySkillIdVisibilityPatchMutation` | `PATCH /skills/registry/{id}/visibility` | Promote/demote |

### Types
| Type | Purpose |
|------|---------|
| `SkillSearchResult` | Single search/browse result |
| `SkillSearchResponse` | Response wrapper (data[], count, search_type) |
| `SkillImportRequest` | Import request body |
| `SkillImportResponse` | Import result (includes security verdict) |
| `SkillSecurityVerdict` | Assessment (verdict, indicators, reasoning) |
| `SkillSecurityIndicator` | Individual risk indicator |
| `ActivateRequest` | Activate request body |
| `VisibilityUpdate` | Visibility change body |

### Existing UI Components (shadcn/ui)
Card, Badge, Button, Input, Select, Dialog, AlertDialog, Tabs, Tooltip, Skeleton, ScrollArea, Table, Sheet

### Existing Patterns
- TanStack React Query with generated `*Options()` hooks
- URL search params for state (useSearchParams)
- Lucide React icons
- Sonner toasts for notifications
- useMutation with onSuccess invalidation
