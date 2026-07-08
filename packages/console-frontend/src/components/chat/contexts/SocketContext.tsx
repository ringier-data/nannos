// Shim: the socket context now lives in the Embed SDK (core TransportClient +
// React provider). Console-only channels use useSocket().onEvent.
export { SocketProvider, useSocket } from '@nannos/embed-sdk';
export type { ConversationSnapshotData } from '@nannos/embed-sdk';
