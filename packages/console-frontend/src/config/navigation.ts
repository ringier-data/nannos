import {
  MessagesSquare,
  Settings,
  Bot,
  Users,
  UsersRound,
  FileText,
  DollarSign,
  CreditCard,
  Calendar,
  Webhook,
  LibraryBig,
  Bug,
  KeyRound,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

export interface NavItem {
  title: string;
  url: string;
  icon: LucideIcon;
}

export const mainNavItems: NavItem[] = [
  {
    title: 'Settings',
    url: '/app',
    icon: Settings,
  },
  {
    title: 'Chat',
    url: '/app/chat',
    icon: MessagesSquare,
  },
  {
    title: 'Sub-Agents',
    url: '/app/subagents',
    icon: Bot,
  },
  {
    title: 'Usage & Costs',
    url: '/app/usage',
    icon: DollarSign,
  },
  {
    title: 'Scheduler',
    url: '/app/scheduler',
    icon: Calendar,
  },
  {
    title: 'Catalogs',
    url: '/app/catalogs',
    icon: LibraryBig,
  },
  {
    title: 'Delivery Channels',
    url: '/app/delivery-channels',
    icon: Webhook,
  },
];

export const groupManagerNavItems: NavItem[] = [
  {
    title: 'Groups',
    url: '/app/groups',
    icon: UsersRound,
  },
];

export const adminNavItems: NavItem[] = [
  {
    title: 'Users',
    url: '/app/admin/users',
    icon: Users,
  },
  {
    title: 'Groups',
    url: '/app/admin/groups',
    icon: UsersRound,
  },
  {
    title: 'Audit Logs',
    url: '/app/admin/audit',
    icon: FileText,
  },
  {
    title: 'Rate Cards',
    url: '/app/admin/rate-cards',
    icon: CreditCard,
  },
  {
    title: 'Bug Reports',
    url: '/app/admin/bug-reports',
    icon: Bug,
  },
  {
    title: 'SCIM Tokens',
    url: '/app/admin/scim-tokens',
    icon: KeyRound,
  },
];
