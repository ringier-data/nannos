# Progress: Skills Registry Frontend

## Phase 1: SkillImportPanel — Search & Results
- [ ] Create `src/components/skills/SkillImportPanel.tsx` with search input + source toggle
- [ ] Add search results grid using `searchSkillsApiV1SkillsRegistrySearchGetOptions`
- [ ] Add loading skeletons and empty state
- [ ] Add "Import" button to SkillsPage sidebar
- [ ] Wire panel toggle state in SkillsPage

## Phase 2: Skill Preview & Security Badge
- [ ] Create `src/components/skills/SecurityVerdictBadge.tsx`
- [ ] Add preview expansion/section for selected result
- [ ] Fetch detail via `getSkillDetailApiV1SkillsRegistryDetailSkillIdGetOptions`
- [ ] Display SKILL.md content, file list, security assessment

## Phase 3: Import Action
- [ ] Add scope selector (personal/group) to import button
- [ ] Call `importSkillApiV1SkillsRegistryImportPost` mutation
- [ ] Handle success (toast, invalidate skills list, switch to imported skill)
- [ ] Handle 403 unsafe (show security warning + optional force)
- [ ] Handle 409 conflict (show overwrite option)

## Phase 4: Browse Repo Mode
- [ ] Add owner/repo input field with validation
- [ ] Wire `browseRepoApiV1SkillsRegistryBrowseGetOptions`
- [ ] Share preview/import flow with Phase 1 results

## Phase 5: Registry Admin Panel (optional)
- [ ] Decide placement (tab in SkillsPage vs admin route)
- [ ] List registry entries with management actions
- [ ] Wire remove, visibility, activate mutations
