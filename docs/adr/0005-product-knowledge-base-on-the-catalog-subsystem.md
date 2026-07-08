---
status: accepted (2026-07-07)
---

# The product knowledge base is a Catalog; ontology explanation rides the ontology skill

Embedded Nannos gains a **knowledge / Q&A tier** — answering questions *about the
host application* — split into two content categories with two different homes.
**(3) Domain/ontology explanation** ("how does a Topic relate to an Audience?")
needs **no new pipeline**: it rides the existing codebase-derived, HITL-curated
**ontology skill** the domain agent already loads to *act*; explaining a concept is
a *usage* of that skill, enabled by one line of embedded-mode framing.
**(1) Product/help knowledge** (how-to, troubleshooting, policy) is delivered as an
instance of the existing **Catalog subsystem** (`console-backend/.../catalog/`,
`agent_common/core/catalog_tools.py`): a named document collection with its own
S3-Vectors index, fed by a pluggable `CatalogSourceAdapter` through a sync pipeline
that already does two-pass contextual retrieval, incremental sync, content-hash
dedup, and deletion. Retrieval is **already wired** — `catalog_search` is an
essential tool on every dynamic agent (`dynamic_agent._get_effective_tools`), so the
embedded domain agent has it by construction. Data-grounding Q&A ("what campaigns do
*I* have") is explicitly **not** this tier — that is the grounding / act-on-behalf
tiers.

## Why

- **Reuse the substrate that already solved the hard parts.** Contextual chunking,
  incremental sync, dedup, per-collection vector index, and a per-user-scoped
  retrieval tool all exist in the Catalog feature (built for the pitch-deck agent).
  Product-help is "point a catalog at the docs" — near-zero new code — rather than a
  fresh RAG store.
- **Freshness is engineered, not bolted on.** Incremental sync + content-hash means
  a re-sync is the freshness mechanism; the agent cites the catalog `source_ref` for
  honesty. This is why the "navigate the UI to validate the docs" idea was rejected
  (see Alternatives) — it is reactive, per-answer, and expensive.
- **Ontology explanation is free.** The ontology skill already encodes objects,
  scopes, and relationships to enable *action*; the same content answers *"what is
  X / how does X relate to Y"*. No second artifact, no drift.
- **Minimal integration effort — the stated goal.** For a host whose docs already
  live in Google Drive, the tier is essentially already built (create catalog, grant
  group read, sync). The one headline build for arbitrary hosts is a single source
  adapter (see Consequences), on a pipeline that already does everything downstream.

## Considered options / alternatives

- **Product docs as a content-pinned skill (like the ontology).** Rejected: skills
  are small, curated, *always loaded* (progressive-disclosure index); product docs
  are large, external, and refreshable — wrong shape. Kept for ontology only. Clean
  split: **ontology = skill, product docs = catalog.**
- **A hand-built `docstore_search` namespace** (`(assistant_id, "documents")` +
  a new external-docs ingestion path). Rejected once the Catalog subsystem was found:
  it would re-implement chunking/sync/dedup at lower quality (plain chunking vs.
  two-pass contextual retrieval) and risk clobbering agent-written channel memories
  that share that namespace. Catalogs have their own per-collection index.
- **Generic web crawling** for "point at your docs site". Rejected as the docs-tier
  cousin of the `DOM-as-ontology` anti-pattern: fragile, defeated by JS-rendered
  SPAs, and dominated by boilerplate-stripping. Every source must be a
  **host-curated, clean-markdown artifact at a stable locator**, never rendered HTML.
- **`navigate`-to-validate-docs** as the anti-staleness mechanism. Rejected:
  reactive (only catches drift on questions users happen to ask), slow/expensive
  (a UI drive per answer), when incremental re-sync + citation already give freshness
  cheaply. Active `navigate` is reserved for explicit *guided walkthroughs*.
- **App-scoped binding now** (`knowledge_catalog_ids` declared on the Domain Adapter,
  auto-equipping the agent independent of per-user ACLs). Deferred, not rejected:
  v1 uses **group-scoped, manually aligned** access (grant the embedded group `read`
  on the KB catalog; `catalog_search` resolves it via the on-behalf-of user's
  accessible catalogs). Safe to defer because product docs are non-sensitive and
  identical per user, so group-read is not a privacy boundary. Revisit when embedding
  scales past manual alignment or a *user-private* KB is needed.

## Consequences

- **v1 sourcing = Google Drive only** (existing adapter, zero new code) to prove
  ontology-skill + KB-catalog + embedded-agent end-to-end.
- **Follow-up 1 (primary): a GitHub-markdown `CatalogSourceAdapter`** — reuses the
  existing GitHub service-account integration (auth already shipped); `{repo, path,
  branch}` → `**/*.md(x)`; `detect_changes` via git commit-compare (precise, cheap);
  covers **private / first-party** docs (console-frontend, cockpit).
- **Follow-up 2: an llms.txt adapter** — single-URL fetch, prefer self-contained
  `llms-full.txt`, else `llms.txt` + linked `.md`; zero-auth, best for **public /
  third-party** hosts. (Its "de facto standard" status is irrelevant — a cooperating
  host publishes one file for Nannos on request.)
- **Pipeline already tolerates text sources (verified):** thumbnails fail open and
  `get_thumbnail` may return `None` (thumbnails are UI-only; retrieval indexes
  `text_content`); one `ExtractedPage` per file is valid. A markdown adapter
  implements `list_files`/`extract_pages`/`detect_changes` and no-ops thumbnails.
- **Minor refactor when a non-Drive adapter lands:** `list_shared_drives` is
  Drive-specific but sits on the base `CatalogSourceAdapter` ABC — pull it off.
- **Known v1 over-reach (accepted):** `catalog_search` sees *all* the on-behalf
  user's accessible catalogs, not just the app KB. Read-only over the user's own
  catalogs; tightened later by the deferred app-scoped binding.
- **Enable category-3 explanation** with a line in the embedded-mode framing telling
  the agent it may *explain* ontology concepts, not only act on them.

See CONTEXT.md "Knowledge / Q&A tier" and "Product KB = a Catalog".
