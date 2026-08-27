/**
 * The input: a textarea + button composition over the ui primitives (the
 * vendored prompt-input carries its own attachment pipeline, which would fight
 * `useAttachments`' REST-upload flow). Enter sends — and while a turn is
 * streaming, sending STEERS by default (`chat.send` handles that) — or, in the
 * viewer's chosen send mode, stops the turn and starts over — so the input
 * never locks. Files arrive via the picker, drag-drop, or a pasted image. A seeded
 * prompt with `sendOnOpen: false` lands here as an editable draft.
 *
 * The box stacks three rows inside ONE border:
 *   row 0 — attachments (absent while nothing is attached);
 *   row 1 — the textarea, with the mic as its only trailing button;
 *   row 2 — attach (left) · current context (stretches, text left-aligned) ·
 *           apply mode · stop/send (right).
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import type { ChangeEvent, DragEvent, ClipboardEvent, KeyboardEvent } from 'react';
import { ArrowUpIcon, FileTextIcon, PaperclipIcon, XIcon } from 'lucide-react';
import { StopIcon } from '../../components/icons/stop-icon';
import { Button } from '../../components/ui/button';
import { Spinner } from '../../components/ui/spinner';
import { Textarea } from '../../components/ui/textarea';
import { cn } from '../../lib/utils';
import { format, useAssistant, useStrings } from '../../react';
import { useChatEngineOptional } from '../engine';
import { useAttachments } from '../hooks/use-attachments';
import type { UseNannosChatValue } from '../hooks/use-nannos-chat';
import { ApplyModeSwitch } from './apply-mode-switch';
import { SendModeSwitch } from './send-mode-switch';
import { useSendMode } from '../send-mode';
import { AudioRecorderButton } from './audio-recorder';

export interface ComposerProps {
  chat: UseNannosChatValue;
  className?: string;
}

export function Composer({ chat, className }: ComposerProps) {
  const strings = useStrings();
  const assistant = useAssistant();
  const attachments = useAttachments();
  const engine = useChatEngineOptional();
  const { mode: sendMode } = useSendMode();
  const [text, setText] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Autofocus. Hosts that mount the panel only while it is open get the mount
  // run; hosts that keep a closed panel mounted get the close→open transition.
  // A close never pulls focus back.
  const isOpen = assistant.isOpen;
  useEffect(() => {
    textareaRef.current?.focus();
  }, []);
  useEffect(() => {
    if (isOpen) textareaRef.current?.focus();
  }, [isOpen]);

  // "New chat": the header/sidebar button bumps the engine's focus signal. The
  // composer is NOT keyed by conversation, so a leftover draft survives the
  // switch — focus it and SELECT it, so the first keystroke replaces it.
  const composerFocus = engine?.composerFocus;
  const subscribeFocus = useCallback(
    (listener: () => void) => composerFocus?.subscribe(listener) ?? (() => {}),
    [composerFocus],
  );
  const getFocusTick = useCallback(() => composerFocus?.getSnapshot() ?? 0, [composerFocus]);
  const focusTick = useSyncExternalStore(subscribeFocus, getFocusTick, () => 0);
  const seenFocusTick = useRef(focusTick);
  useEffect(() => {
    if (focusTick === seenFocusTick.current) return; // mount, not a request
    seenFocusTick.current = focusTick;
    const el = textareaRef.current;
    if (!el) return;
    el.focus();
    el.select(); // no-op while the draft is empty
  }, [focusTick]);

  // Draft seeding: a prompt opened with `sendOnOpen: false` prefills the
  // textarea; the user edits and presses send themselves.
  const seededPrompt = assistant.seededPrompt;
  const clearSeededPrompt = assistant.clearSeededPrompt;
  useEffect(() => {
    if (seededPrompt && seededPrompt.sendOnOpen === false) {
      // A `newConversation` draft retargets first, so the prefilled question
      // sits in a fresh thread, not on top of whatever chat was active.
      if (seededPrompt.newConversation && engine) {
        engine.conversations.resolveTarget(seededPrompt.contextKey, { fresh: true });
      }
      setText(seededPrompt.text);
      clearSeededPrompt();
    }
  }, [seededPrompt, clearSeededPrompt, engine]);

  const isReadOnly = chat.isReadOnly;
  const hasUploading = attachments.items.some((item) => item.status === 'uploading');
  // Stop replaces send only while the turn runs AND the draft is empty.
  const showStop = chat.isBusy && !text.trim();
  // What the chip shows: the LIVE page context the host publishes on navigation
  // (provider state, so the composer re-renders as the user moves through the
  // app) — that is also what the next send will carry to the agent. Hosts that
  // publish none fall back to the key the conversation was OPENED under
  // (`campaign:123`), which is fixed for the conversation's life.
  const pageContext = assistant.pageContext;
  const conversationKey = engine?.conversations.contextKeyOf(chat.conversationId);
  const contextLabel = pageContext ? (pageContext.title ?? pageContext.key) : conversationKey;
  const contextTitle = pageContext?.key ?? conversationKey;

  const submit = () => {
    const value = text.trim();
    if (!value || isReadOnly || hasUploading) return;
    chat.send(value, {
      files: attachments.readyFiles(),
      // Only meaningful while a turn runs; a plain send otherwise.
      interrupt: chat.isBusy && sendMode === 'stop-and-send',
    });
    attachments.clear();
    setText('');
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  const addFiles = (files: File[]) => {
    if (files.length === 0 || isReadOnly) return;
    void attachments.add(chat.conversationId, files);
  };

  const handleFilePick = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files ?? []));
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    if (event.dataTransfer.files.length === 0) return;
    event.preventDefault();
    addFiles(Array.from(event.dataTransfer.files));
  };

  const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const images = Array.from(event.clipboardData.files).filter((file) =>
      file.type.startsWith('image/'),
    );
    if (images.length === 0) return;
    event.preventDefault();
    addFiles(images);
  };

  return (
    <div
      data-slot="nannos-composer"
      className={cn('p-1.5', className)}
      onDragOver={(event) => event.preventDefault()}
      onDrop={handleDrop}
    >
      {isReadOnly && (
        <p className="px-1 pb-1 text-muted-foreground text-xs">{strings['composer.readOnly']}</p>
      )}

      {/* The VISUAL input box (the textarea inside is border-0) — border-input,
          not bare border, so the `--input` theme token governs it (shadcn
          convention; identical to --border in the default palette). The focus
          glow lives here too, for the same reason: the inner textarea is
          `focus-visible:ring-0`, so the box wears the ring the ui/input
          primitive would draw on itself (same `ring-ring/50` + 3px). */}
      <div
        data-slot="nannos-composer-box"
        className="rounded-lg border border-input bg-background transition-[color,box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/50"
      >
        {/* Row 0 — attachments. Rendered only while something is attached. */}
        {attachments.items.length > 0 && (
          <div
            data-slot="nannos-composer-attachments"
            className="flex flex-wrap gap-1 border-b px-1.5 py-1"
          >
            {attachments.items.map((item) => (
              <span
                key={item.key}
                data-slot="nannos-attachment"
                className={cn(
                  'inline-flex max-w-full items-center gap-1.5 rounded-md border bg-secondary px-1.5 py-0.5 text-secondary-foreground text-xs',
                  item.status === 'error' && 'border-destructive text-destructive',
                )}
              >
                {item.status === 'uploading' && (
                  <>
                    <Spinner className="size-3" />
                    <span className="sr-only">{strings['attachments.uploading']}</span>
                  </>
                )}
                <span className="truncate">
                  {item.status === 'error'
                    ? `${item.name} — ${strings['attachments.failed']}`
                    : item.name}
                </span>
                <button
                  type="button"
                  aria-label={strings['attachments.remove']}
                  className="shrink-0 rounded-sm opacity-70 transition-opacity hover:opacity-100"
                  onClick={() => attachments.remove(item.key)}
                >
                  <XIcon className="size-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        {/* Row 1 — the text input. The mic is the only button that lives here. */}
        <div data-slot="nannos-composer-input-row" className="flex items-end gap-1 p-1">
          <Textarea
            ref={textareaRef}
            data-slot="nannos-composer-input"
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder={
              chat.isBusy ? strings['composer.placeholderStreaming'] : strings['composer.placeholder']
            }
            disabled={isReadOnly}
            rows={1}
            className="max-h-40 min-h-8 flex-1 resize-none border-0 bg-transparent px-2 py-1.5 shadow-none focus-visible:ring-0"
          />

          {/* Recordings upload through the same attachment pipeline as picked files. */}
          <AudioRecorderButton disabled={isReadOnly} onRecorded={(file) => addFiles([file])} />
        </div>

        {/* Row 2 — attach · context · apply mode · send. `relative` anchors the
            apply-mode menu, which hangs ABOVE this row: positioning against the
            row (rather than portalling) keeps it inside the shadow root and
            bounds its width to the panel. The composer box has no overflow clip,
            so it is free to overhang upward. */}
        <div
          data-slot="nannos-composer-actions"
          className="relative flex items-center gap-1 border-t p-1"
        >
          <input ref={fileInputRef} type="file" multiple hidden onChange={handleFilePick} />
          <Button
            data-slot="nannos-composer-attach"
            type="button"
            variant="ghost"
            size="icon-sm"
            className="size-7"
            aria-label={strings['composer.attach']}
            disabled={isReadOnly}
            onClick={() => fileInputRef.current?.click()}
          >
            <PaperclipIcon />
          </Button>

          {/* Stretches so send stays hard right; its own text is left-aligned. */}
          <div
            data-slot="nannos-composer-context"
            className="flex min-w-0 flex-1 items-center justify-start"
          >
            {contextLabel && (
              <span
                className="flex min-w-0 items-center gap-1.5 border-l pl-1.5 text-muted-foreground text-xs"
                title={contextTitle}
                aria-label={format(strings['context.label'], { label: contextLabel })}
              >
                <FileTextIcon aria-hidden="true" className="size-3.5 shrink-0" />
                <span className="truncate">{contextLabel}</span>
              </span>
            )}
          </div>

          {/* Directly left of send: whether a form fill asks first is answered
              where the user is when they ask for one. Absent when the host
              fixed the mode, or when no client object is registered — with
              nothing to apply into, there is no choice to make. */}
          <ApplyModeSwitch />

          {/* While a turn runs, what the NEXT send does to it — steer it, or
              stop it and start over. Gone when nothing runs: both are then the
              same plain send. */}
          {chat.isBusy && <SendModeSwitch />}

          {/* One button, never both: a running turn offers STOP until the user
              starts typing — from that moment the only useful action is to
              send, which STEERS the turn instead of interrupting it. */}
          {showStop ? (
            <Button
              data-slot="nannos-composer-stop"
              type="button"
              size="icon-sm"
              className="size-7"
              aria-label={strings['composer.stop']}
              onClick={() => void chat.stop()}
            >
              <StopIcon />
            </Button>
          ) : (
            <Button
              data-slot="nannos-composer-send"
              type="button"
              size="icon-sm"
              className="size-7"
              aria-label={strings['composer.send']}
              disabled={isReadOnly || !text.trim() || hasUploading}
              onClick={submit}
            >
              <ArrowUpIcon />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
