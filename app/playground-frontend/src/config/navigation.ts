import { MessagesSquare, Settings, Bot, Users, UsersRound, FileText, DollarSign, CreditCard } from 'lucide-react';
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
];
