/**
 * The conversation list + selection, over the scope's ConversationsStore.
 */
import { useCallback, useSyncExternalStore } from 'react';
import { useChatEngine } from '../engine';
import type { ConversationMeta } from '../../transport';

export interface UseConversationsValue {
  conversations: ConversationMeta[];
  activeConversationId: string | null;
  isLoading: boolean;
  selectConversation: (id: string) => void;
  createConversation: () => string;
  loadConversations: (search?: string) => Promise<void>;
  /** Remove a conversation from the history (server-side soft delete).
   *  Resolves false when the server refused and the row was put back. */
  deleteConversation: (id: string) => Promise<boolean>;
  /** Rename a conversation. Resolves false when the server refused and the row
   *  went back to its old name. */
  renameConversation: (id: string, title: string) => Promise<boolean>;
  /** A conversation owned by another embedded surface is read-only here. */
  isReadOnly: (id: string) => boolean;
}

export function useConversations(): UseConversationsValue {
  const { conversations, composerFocus } = useChatEngine();
  const snapshot = useSyncExternalStore(conversations.subscribe, conversations.getSnapshot);

  // Switching conversations — picking one from the history/sidebar/continue
  // card, or starting a new chat — also hands the caret back to the composer
  // (and selects whatever draft is still in it): landing on a conversation
  // means starting to type.
  const selectConversation = useCallback(
    (id: string) => {
      conversations.select(id);
      composerFocus.request();
    },
    [conversations, composerFocus],
  );
  const createConversation = useCallback(() => {
    const id = conversations.create();
    composerFocus.request();
    return id;
  }, [conversations, composerFocus]);
  const loadConversations = useCallback(
    (search?: string) => conversations.loadList(search),
    [conversations],
  );
  const deleteConversation = useCallback(
    (id: string) => conversations.remove(id),
    [conversations],
  );
  const renameConversation = useCallback(
    (id: string, title: string) => conversations.rename(id, title),
    [conversations],
  );
  const isReadOnly = useCallback((id: string) => conversations.isReadOnly(id), [conversations]);

  return {
    conversations: snapshot.items,
    activeConversationId: snapshot.activeId,
    isLoading: snapshot.isLoading,
    selectConversation,
    createConversation,
    loadConversations,
    deleteConversation,
    renameConversation,
    isReadOnly,
  };
}
