/**
 * Read the rendered page as a markdown outline, so every host page answers
 * `read_current_page` without registering a reader.
 *
 * The walk covers what the user can actually see: an element hidden by CSS, an
 * unmounted tab, a closed accordion contribute nothing, because they are not
 * what the user is asking about. Headings give the outline its levels — real
 * `h1`–`h6`, `role="heading"`, and shadcn-style card titles (`data-slot=
 * "card-title"`). Tables come out as markdown tables — both real `<table>`s and
 * ARIA grids (`role="grid"`, MUI X DataGrid renders these) — form controls as
 * their current value, and icon-only controls as their accessible label.
 *
 * Two attributes let hosts steer the walk:
 * - `data-nannos-ignore` drops an element and everything under it. (The SDK's
 *   own panel needs no marking — it renders in a shadow root, which the walk
 *   never enters — but its host element carries it anyway.)
 * - `data-nannos-redact` replaces an element's content with "[redacted]". It
 *   belongs on anything that can hold a secret's value — the outline ships
 *   whatever is rendered, so unlike a page reader there is no field-by-field
 *   allowlist between the screen and the model.
 *
 * A host can also mark the region worth reading with `data-nannos-read-root`;
 * without it the walk starts at `<main>`, or failing that `<body>`.
 *
 * The result is capped by the caller's budget, cut mid-walk rather than after,
 * so a page with an enormous table cannot make the walk build a report only to
 * throw most of it away.
 */

export const NANNOS_IGNORE_ATTRIBUTE = 'data-nannos-ignore';
export const NANNOS_REDACT_ATTRIBUTE = 'data-nannos-redact';
export const NANNOS_READ_ROOT_ATTRIBUTE = 'data-nannos-read-root';

const REDACTED = '[redacted]';

/** Machinery, media and vector drawings: nothing in them reads as text. */
const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'IFRAME', 'OBJECT', 'EMBED', 'CANVAS', 'VIDEO', 'AUDIO', 'SVG', 'DIALOG']);

/**
 * Elements whose text joins the line around them rather than starting one.
 * Judged by tag rather than by computed style: one getComputedStyle per node
 * would make the walk pay layout for every element, and in the UI kits this
 * SDK meets a div is a block and a span is not, whatever flex classes they carry.
 */
const INLINE_TAGS = new Set([
  'A', 'ABBR', 'B', 'BDI', 'BDO', 'BUTTON', 'CITE', 'CODE', 'DATA', 'DFN', 'EM', 'I', 'IMG', 'KBD',
  'LABEL', 'MARK', 'OUTPUT', 'Q', 'S', 'SAMP', 'SMALL', 'SPAN', 'STRONG', 'SUB', 'SUP', 'TIME', 'U', 'VAR', 'WBR',
]);

const MAX_LINE = 400;
const MAX_TABLE_ROWS = 40;
/** The tail rather than the head, because a `pre` on a dashboard is a log more often than code. */
const MAX_PRE_LINES = 25;

type Walk = {
  lines: string[];
  spent: number;
  budget: number;
  truncated: boolean;
  /** Skeletons seen, so the outline can say "still loading" instead of reading as an empty page. */
  skeletons: number;
};

/**
 * Whether the user can see this element. `checkVisibility` answers for
 * `display`, `visibility` and `opacity: 0` in one call; where it does not exist
 * (older browsers, a test DOM) the element is assumed visible, because
 * reporting too much is better than reporting a blank page.
 */
function isHidden(element: Element): boolean {
  if (element.getAttribute('aria-hidden') === 'true') {
    return true;
  }
  const check = (element as Element & { checkVisibility?: (options?: object) => boolean }).checkVisibility;
  if (typeof check !== 'function') {
    return false;
  }
  // Both spellings, because the option names were renamed after browsers first shipped them.
  return !check.call(element, { checkOpacity: true, checkVisibilityCSS: true, opacityProperty: true, visibilityProperty: true });
}

/** A loading placeholder: shadcn renders `data-slot="skeleton"`, MUI a `MuiSkeleton-root` class. */
function isSkeleton(element: Element): boolean {
  return element.getAttribute('data-slot') === 'skeleton' || element.classList.contains('MuiSkeleton-root');
}

function isSkipped(element: Element, walk: Walk): boolean {
  if (SKIP_TAGS.has(element.tagName.toUpperCase())) {
    return true;
  }
  if (element.hasAttribute(NANNOS_IGNORE_ATTRIBUTE)) {
    return true;
  }
  // Screen-reader-only text doubles what the visible text already says; the labels the walk wants
  // (on icon-only controls) come from aria-label instead.
  if (element.classList.contains('sr-only')) {
    return true;
  }
  // The page context already carries the breadcrumb trail, and the footer says the same on every
  // page; both would only spend the outline's budget on what the model already has. Breadcrumbs
  // by slot (shadcn) or by the ARIA name both MUI and shadcn give the nav.
  if (element.tagName === 'FOOTER' || element.getAttribute('data-slot') === 'breadcrumb') {
    return true;
  }
  if (element.tagName === 'NAV' && element.getAttribute('aria-label')?.toLowerCase() === 'breadcrumb') {
    return true;
  }
  if (isSkeleton(element)) {
    walk.skeletons += 1;
    return true;
  }
  return isHidden(element);
}

function push(walk: Walk, line: string) {
  if (walk.truncated) {
    return;
  }
  const collapsed = line.replace(/\s+/g, ' ').trim().slice(0, MAX_LINE);
  if (!collapsed) {
    // A blank line is spacing before a heading; two in a row say nothing more than one.
    if (line === '' && walk.lines.length && walk.lines[walk.lines.length - 1] !== '') {
      walk.lines.push('');
    }
    return;
  }
  if (walk.spent + collapsed.length > walk.budget) {
    walk.truncated = true;
    return;
  }
  walk.lines.push(collapsed);
  walk.spent += collapsed.length + 1;
}

/**
 * What a form control is worth as text: its value. Undefined for anything that is not a control.
 * A password never leaves as its value, whatever attributes it carries.
 */
function controlText(element: Element): string | undefined {
  const role = element.getAttribute('role');
  // Radix renders a switch or a checkbox as a button, with the state in aria-checked.
  if (role === 'switch') {
    return element.getAttribute('aria-checked') === 'true' ? '[on]' : '[off]';
  }
  if (role === 'checkbox' || role === 'radio') {
    return element.getAttribute('aria-checked') === 'true' ? '[x]' : '[ ]';
  }
  if (role === 'progressbar') {
    const now = element.getAttribute('aria-valuenow');
    return now === null ? '[progress]' : `[progress ${now}/${element.getAttribute('aria-valuemax') ?? '100'}]`;
  }
  if (element instanceof HTMLInputElement) {
    if (element.type === 'checkbox' || element.type === 'radio') {
      return element.checked ? '[x]' : '[ ]';
    }
    if (element.type === 'password') {
      return '[hidden]';
    }
    if (element.type === 'hidden') {
      return '';
    }
    if (element.type === 'submit' || element.type === 'button') {
      return element.value;
    }
    if (!element.value) {
      return element.placeholder ? `[empty — ${element.placeholder.slice(0, 60)}]` : '[empty]';
    }
    return `[${element.value.slice(0, 120)}]`;
  }
  if (element instanceof HTMLTextAreaElement) {
    return element.value ? `[${element.value.slice(0, 200)}]` : '[empty]';
  }
  if (element instanceof HTMLSelectElement) {
    return `[${element.selectedOptions[0]?.textContent?.trim() ?? ''}]`;
  }
  return undefined;
}

/** Flatten an element to one run of text: for a heading, a table cell, a list item, a label. */
function inlineText(element: Element, walk: Walk): string {
  if (element.hasAttribute(NANNOS_REDACT_ATTRIBUTE)) {
    return REDACTED;
  }
  const control = controlText(element);
  if (control !== undefined) {
    return control;
  }
  let text = '';
  for (const node of element.childNodes) {
    if (node.nodeType === Node.TEXT_NODE) {
      text += node.textContent ?? '';
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      const child = node as Element;
      if (!isSkipped(child, walk)) {
        text += ` ${inlineText(child, walk)} `;
      }
    }
  }
  if (!text.trim()) {
    if (element instanceof HTMLImageElement) {
      return element.alt ? `(${element.alt})` : '';
    }
    // An icon-only control still tells a screen reader what it does; tell the model the same way.
    const label = element.getAttribute('aria-label') ?? element.getAttribute('title');
    if (label) {
      return `[${label}]`;
    }
  }
  return text;
}

/**
 * The outline level of a heading, or undefined for everything else. A shadcn
 * card title is a `div`, so it is recognized by its slot and levelled by how
 * deeply its card sits in other cards: the page's `h1` is level 1, a card on
 * the page is level 2, a card within that card level 3.
 */
function headingLevel(element: Element, cardDepth: number): number | undefined {
  const tagMatch = /^H([1-6])$/.exec(element.tagName);
  if (tagMatch) {
    return Number(tagMatch[1]);
  }
  if (element.getAttribute('role') === 'heading') {
    return Number(element.getAttribute('aria-level')) || 2;
  }
  if (element.getAttribute('data-slot') === 'card-title') {
    return Math.min(1 + Math.max(cardDepth, 1), 6);
  }
  return undefined;
}

/** Emit rows already gathered (from a real table or an ARIA grid) as a markdown table. */
function pushRows(rows: Array<{ cells: string[]; isHeader: boolean }>, total: number, walk: Walk) {
  if (!rows.length) {
    return;
  }
  push(walk, '');
  rows.forEach((row, index) => {
    if (!row.cells.some(Boolean)) {
      return;
    }
    push(walk, `| ${row.cells.join(' | ')} |`);
    if (index === 0 && row.isHeader) {
      push(walk, `|${' --- |'.repeat(row.cells.length)}`);
    }
  });
  if (total > rows.length) {
    push(walk, `(… ${total - rows.length} more rows)`);
  }
}

function walkTable(table: HTMLTableElement, walk: Walk) {
  const rows = [...table.rows].filter(row => !isSkipped(row, walk));
  pushRows(
    rows.slice(0, MAX_TABLE_ROWS).map(row => {
      const cells = [...row.cells].filter(cell => !isSkipped(cell, walk));
      return {
        cells: cells.map(cell => inlineText(cell, walk).replace(/\|/g, '/').trim()),
        isHeader: row.parentElement?.tagName === 'THEAD' || (cells.length > 0 && cells.every(cell => cell.tagName === 'TH')),
      };
    }),
    rows.length,
    walk,
  );
}

/**
 * A table built out of divs: `role="grid"` / `role="treegrid"` / `role="table"`,
 * which is what MUI X DataGrid and similar virtualized kits render. Only the
 * rows actually mounted appear — with virtualization that is exactly the part
 * of the table the user can see.
 */
function walkAriaGrid(grid: Element, walk: Walk) {
  const rows = [...grid.querySelectorAll('[role="row"]')].filter(row => !isSkipped(row, walk));
  pushRows(
    rows.slice(0, MAX_TABLE_ROWS).map(row => {
      const cells = [...row.querySelectorAll('[role="columnheader"], [role="rowheader"], [role="gridcell"], [role="cell"]')]
        .filter(cell => !isSkipped(cell, walk));
      return {
        cells: cells.map(cell => inlineText(cell, walk).replace(/\|/g, '/').trim()),
        isHeader: cells.length > 0 && cells.every(cell => cell.getAttribute('role') === 'columnheader'),
      };
    }),
    rows.length,
    walk,
  );
}

function walkDefinitionList(list: Element, walk: Walk) {
  let term = '';
  for (const child of list.children) {
    if (isSkipped(child, walk)) {
      continue;
    }
    if (child.tagName === 'DT') {
      term = inlineText(child, walk).trim();
    } else if (child.tagName === 'DD') {
      push(walk, `- ${term ? `${term}: ` : ''}${inlineText(child, walk).trim()}`);
      term = '';
    }
  }
}

/** Bullets, flattened: a nested list becomes more bullets rather than deeper ones. */
function walkList(list: Element, walk: Walk) {
  for (const item of list.children) {
    if (isSkipped(item, walk)) {
      continue;
    }
    let text = '';
    const nested: Element[] = [];
    for (const node of item.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) {
        text += node.textContent ?? '';
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        const child = node as Element;
        if (isSkipped(child, walk)) {
          continue;
        }
        if (child.tagName === 'UL' || child.tagName === 'OL') {
          nested.push(child);
        } else {
          text += ` ${inlineText(child, walk)}`;
        }
      }
    }
    if (text.trim()) {
      push(walk, `- ${text}`);
    }
    nested.forEach(sub => walkList(sub, walk));
  }
}

/** A `pre` is a log more often than it is code here, so the tail is the part worth keeping. */
function walkPre(pre: Element, walk: Walk) {
  const lines = (pre.textContent ?? '').split('\n').map(line => line.trim()).filter(Boolean);
  if (!lines.length) {
    return;
  }
  push(walk, '```');
  if (lines.length > MAX_PRE_LINES) {
    push(walk, `(… ${lines.length - MAX_PRE_LINES} earlier lines)`);
  }
  lines.slice(-MAX_PRE_LINES).forEach(line => push(walk, line));
  push(walk, '```');
}

function walkTablist(tablist: Element, walk: Walk) {
  const tabs = [...tablist.querySelectorAll('[role="tab"]')].filter(tab => !isSkipped(tab, walk));
  if (!tabs.length) {
    return;
  }
  const labels = tabs.map(tab => {
    const label = inlineText(tab, walk).trim();
    return tab.getAttribute('aria-selected') === 'true' ? `${label} (open)` : label;
  });
  push(walk, `Tabs: ${labels.join(' | ')}`);
}

function walkBlock(element: Element, walk: Walk, cardDepth: number) {
  if (walk.truncated) {
    return;
  }
  let line = '';
  const flush = () => {
    if (line.trim()) {
      push(walk, line);
    }
    line = '';
  };

  for (const node of element.childNodes) {
    if (walk.truncated) {
      return;
    }
    if (node.nodeType === Node.TEXT_NODE) {
      line += node.textContent ?? '';
      continue;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) {
      continue;
    }
    const child = node as Element;
    if (isSkipped(child, walk)) {
      continue;
    }
    if (child.hasAttribute(NANNOS_REDACT_ATTRIBUTE)) {
      line += ` ${REDACTED} `;
      continue;
    }

    const heading = headingLevel(child, cardDepth);
    if (heading !== undefined) {
      flush();
      push(walk, '');
      push(walk, `${'#'.repeat(Math.min(heading, 6))} ${inlineText(child, walk)}`);
      continue;
    }

    const tag = child.tagName;
    const role = child.getAttribute('role');
    if (tag === 'TABLE') {
      flush();
      walkTable(child as HTMLTableElement, walk);
    } else if (role === 'grid' || role === 'treegrid' || role === 'table') {
      flush();
      walkAriaGrid(child, walk);
    } else if (tag === 'DL') {
      flush();
      walkDefinitionList(child, walk);
    } else if (tag === 'UL' || tag === 'OL' || role === 'list') {
      flush();
      walkList(child, walk);
    } else if (tag === 'PRE') {
      flush();
      walkPre(child, walk);
    } else if (role === 'tablist') {
      flush();
      walkTablist(child, walk);
    } else if (tag === 'HR' || role === 'separator') {
      flush();
    } else if (INLINE_TAGS.has(tag) || controlText(child) !== undefined) {
      const text = inlineText(child, walk);
      if (text.trim()) {
        line += ` ${text}`;
      }
    } else {
      flush();
      walkBlock(child, walk, cardDepth + (child.getAttribute('data-slot') === 'card' ? 1 : 0));
      flush();
    }
  }
  flush();
}

/**
 * The rendered page as a markdown outline, within `maxChars`. Empty when there
 * is nothing to read, which the caller treats as "this page reports nothing"
 * rather than an error. (The SDK's own panel renders in a shadow root, which
 * neither the walk nor the dialog/toast queries below can enter.)
 */
export function snapshotScreenOutline(maxChars: number): string {
  const root =
    document.querySelector(`[${NANNOS_READ_ROOT_ATTRIBUTE}]`) ?? document.querySelector('main') ?? document.body;
  const walk: Walk = { lines: [], spent: 0, budget: Math.max(maxChars, 200), truncated: false, skeletons: 0 };
  walkBlock(root, walk, 0);

  // Dialogs render in portals outside <main>, and an open one is what the user
  // is actually looking at.
  for (const dialog of document.querySelectorAll('[role="dialog"], [role="alertdialog"]')) {
    if (root.contains(dialog) || dialog.closest(`[${NANNOS_IGNORE_ATTRIBUTE}]`) || isSkipped(dialog, walk)) {
      continue;
    }
    push(walk, '');
    push(walk, '## Open dialog');
    walkBlock(dialog, walk, 0);
  }

  // A toast is often the answer to "why did that fail", and it lives outside
  // <main> as well: sonner marks its toasts; MUI/notistack alerts carry
  // role="alert". Anything inside the root was already walked.
  const toasts = [...document.querySelectorAll('[data-sonner-toast], [role="alert"]')].filter(
    toast => !root.contains(toast) && !toast.closest(`[${NANNOS_IGNORE_ATTRIBUTE}]`) && !isSkipped(toast, walk),
  );
  if (toasts.length) {
    push(walk, '');
    push(walk, '## Notifications on screen');
    toasts.forEach(toast => push(walk, `- ${inlineText(toast, walk)}`));
  }

  if (walk.skeletons > 0) {
    push(walk, '');
    push(walk, '(Parts of the page are still loading.)');
  }
  if (walk.truncated) {
    walk.lines.push('… (the rest of the page did not fit)');
  }
  return walk.lines.join('\n').trim();
}
