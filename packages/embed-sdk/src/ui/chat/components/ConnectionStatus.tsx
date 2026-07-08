import { cn } from '@/lib/utils';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { useSocket } from '../contexts';
import type { AgentInfo } from '../types';
import { truncateText } from '../utils';

interface ConnectionStatusProps {
  className?: string;
}

function AgentInfoContent({ agentInfo }: { agentInfo: AgentInfo | null }) {
  if (!agentInfo) return null;

  const safeName = agentInfo.name || 'Unknown Agent';
  const description = agentInfo.description;
  const version = agentInfo.version;
  const skills = agentInfo.skills || [];
  const capabilities = agentInfo.capabilities || {};

  return (
    <div className="space-y-3">
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-semibold text-popover-foreground flex items-center gap-2">
            <span>🤖</span>
            <span>{safeName}</span>
          </div>
          {description && (
            <p className="text-xs text-muted-foreground mt-1 line-clamp-2">📝 {truncateText(description, 100)}</p>
          )}
        </div>
        {version && <Badge variant="secondary">v{version}</Badge>}
      </div>

      {/* Info Rows */}
      <div className="space-y-2 text-xs">
        {agentInfo.url && (
          <div className="flex gap-2">
            <span className="text-muted-foreground">🔗 Endpoint</span>
            <span className="font-mono text-popover-foreground truncate flex-1">{truncateText(agentInfo.url, 40)}</span>
          </div>
        )}
        {agentInfo.protocolVersion && (
          <div className="flex gap-2">
            <span className="text-muted-foreground">🔌 Protocol</span>
            <span className="text-popover-foreground">{agentInfo.protocolVersion}</span>
          </div>
        )}
        {agentInfo.preferredTransport && (
          <div className="flex gap-2">
            <span className="text-muted-foreground">🚀 Transport</span>
            <span className="text-popover-foreground">{agentInfo.preferredTransport}</span>
          </div>
        )}
      </div>

      {/* Capabilities */}
      {(capabilities.pushNotifications || capabilities.streaming) && (
        <>
          <Separator />
          <div>
            <div className="text-xs text-muted-foreground mb-1">⚡ Capabilities</div>
            <div className="flex flex-wrap gap-1">
              {capabilities.pushNotifications && <Badge variant="secondary">Push Notifications</Badge>}
              {capabilities.streaming && <Badge variant="secondary">Streaming</Badge>}
            </div>
          </div>
        </>
      )}

      {/* Skills */}
      {skills.length > 0 && (
        <>
          <Separator />
          <div>
            <div className="text-xs text-muted-foreground mb-1">🎯 Skills ({skills.length})</div>
            <ul className="space-y-1">
              {skills.slice(0, 3).map((skill, i) => (
                <li key={i} className="text-xs text-popover-foreground">
                  <span className="font-medium">{skill.name || skill.id}</span>
                  {skill.description && (
                    <span className="text-muted-foreground ml-1">- {truncateText(skill.description, 50)}</span>
                  )}
                </li>
              ))}
              {skills.length > 3 && <li className="text-xs text-muted-foreground">+{skills.length - 3} more</li>}
            </ul>
          </div>
        </>
      )}
    </div>
  );
}

export function ConnectionStatus({ className }: ConnectionStatusProps) {
  const { isConnected, agentInfo } = useSocket();

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="sm" className={cn('gap-2', className)}>
          <div className={cn('w-2 h-2 rounded-full transition-colors', isConnected ? 'bg-green-500' : 'bg-red-500')} />
          <span className="text-sm">{isConnected ? 'Connected' : 'Disconnected'}</span>
        </Button>
      </PopoverTrigger>
      {isConnected && agentInfo && (
        <PopoverContent className="w-80" align="end">
          <AgentInfoContent agentInfo={agentInfo} />
        </PopoverContent>
      )}
    </Popover>
  );
}
