import { ChatAppWrapper } from '@/components/chat';

export function ChatPage() {
  return (
    <div className="h-full">
      <ChatAppWrapper socketPath="/api/v1/socket.io" />
    </div>
  );
}
