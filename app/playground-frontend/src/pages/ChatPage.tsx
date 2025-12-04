import { ChatAppWrapper } from '@/components/chat';

export function ChatPage() {
  return (
    <div>
      <ChatAppWrapper socketPath="/api/v1/socket.io" />
    </div>
  );
}
