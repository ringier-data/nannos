import { Bug } from 'lucide-react';
import { Switch } from '@/components/ui/switch';
import { useNannosDevMode } from '@/components/nannos/NannosDevMode';

/**
 * Sidebar switch for the assistant's developer mode. It sits inside the Admin
 * Mode group, which is the gate: the switch renders only while an administrator
 * has admin mode on (`useNannosDevMode().available`), so it is never a control
 * a normal user can see and not use.
 *
 * What it turns on is the SDK panel's inspector on the chat page — page
 * context, conversation contextKey, the client-object manifest and the
 * client-action round trips. The panel's own header carries a second, smaller
 * switch that previews the end-user view without leaving dev mode.
 */
export function AssistantDevModeSwitch() {
  const { available, enabled, toggle } = useNannosDevMode();
  if (!available) return null;

  return (
    <label
      htmlFor="assistant-dev-mode"
      // Same metrics as SidebarGroupLabel — it reads as a sibling of the
      // "Admin Mode" row it sits under, not as a nav item.
      className="flex h-8 cursor-pointer select-none items-center justify-between gap-2 px-2 text-xs font-medium text-sidebar-foreground/70"
      title="Show what the console pushes to the agent, on the chat page"
    >
      <span className="flex items-center gap-2">
        <Bug className="h-4 w-4" />
        Assistant Dev Mode
      </span>
      <Switch
        id="assistant-dev-mode"
        checked={enabled}
        onCheckedChange={toggle}
        aria-label="Toggle assistant developer mode"
      />
    </label>
  );
}
