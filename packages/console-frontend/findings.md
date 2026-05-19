# Findings: Skills Registry Frontend

## Discovery

### Existing Infrastructure (complete, no work needed)
- **Generated types** in `types.gen.ts`: SkillSearchResult, SkillSearchResponse, SkillImportRequest, SkillImportResponse, SkillSecurityVerdict, SkillSecurityIndicator, ActivateRequest, VisibilityUpdate — all auto-generated from backend OpenAPI spec.
- **Generated React Query hooks** in `@tanstack/react-query.gen.ts`: searchSkillsOptions, browseRepoOptions, getSkillDetailOptions, importSkillMutation, removeSkillMutation, activateSkillMutation, updateVisibilityMutation — all ready to use.
- **Generated SDK functions** in `sdk.gen.ts`: All registry endpoint wrappers exist.
- **Route exists**: `/app/skills` in App.tsx

### Existing Pages & Components (already built)
- **SkillsPage.tsx** (~800 lines): Full skills management with agent selector, scope tabs (All/Personal/Group/Standard), skill list sidebar, create/delete/rename actions, integrates SkillEditorPanel.
- **SkillEditorPanel.tsx** (~700 lines): Inline editor for a single skill — SKILL.md (description + body) + bundled file management (add/rename/delete files).
- **SkillEditorModal.tsx** (~800 lines): Local-state modal for editing default-scope skills within SubAgent config page.
- **skillFiles.ts**: Manual API helpers for skill file CRUD (list, get, write, delete).

### Frontend Patterns Observed
- TanStack React Query with auto-generated `*Options()` for GET, `*Mutation` for POST/PUT/DELETE
- URL search params via useSearchParams for persistent UI state
- shadcn/ui components (Card, Badge, Select, Dialog, Tabs, Skeleton, etc.)
- Lucide React icons
- Sonner toasts for success/error notifications
- Query invalidation in mutation `onSuccess` callbacks

### What's Missing (needs building)
1. **SkillImportPanel** — Search/browse/preview/import UI (the discovery experience)
2. **SecurityVerdictBadge** — Reusable component for displaying safety assessment
3. **Integration hook** — "Import" button in SkillsPage sidebar + panel toggle state
4. **Registry admin panel** (optional) — manage imported skills (visibility, delete)

### Key Insight: Agent Context Reuse
The existing SkillsPage already has agent selection + group context. The import panel can inherit these values (which agent to import for, which group scope to use) without additional UI work.

### Security UX Consideration
When import returns 403 (unsafe verdict), the UI should:
1. Show the security reasoning clearly
2. Display individual indicators with evidence
3. Only show "Force Import" if user has approver role (check from auth context)
4. Force import requires explicit confirmation (AlertDialog, not just a button)
