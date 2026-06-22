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
  Globe,
  ShieldAlert,
  BookOpen,
  Package,
  BarChart3,
  Cpu,
  Wallet,
  Activity,
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
  {
    title: 'Playbooks',
    url: '/app/playbooks',
    icon: BookOpen,
  },
  {
    title: 'Skill Registry',
    url: '/app/skill-registry',
    icon: Package,
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
    title: 'System Status',
    url: '/app/admin/system-status',
    icon: Activity,
  },
  {
    title: 'Analytics',
    url: '/app/admin/analytics',
    icon: BarChart3,
  },
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
    title: 'Model Gateway',
    url: '/app/admin/model-gateway',
    icon: Cpu,
  },
  {
    title: 'Budget Guard',
    url: '/app/admin/budget-guard',
    icon: Wallet,
  },
  {
    title: 'Bug Reports',
    url: '/app/admin/bug-reports',
    icon: Bug,
  },
  {
    title: 'Inbound SCIM',
    url: '/app/admin/scim-tokens',
    icon: KeyRound,
  },
  {
    title: 'Outbound SCIM',
    url: '/app/admin/outbound-scim',
    icon: Globe,
  },
  {
    title: 'Tool Risk Scores',
    url: '/app/admin/tool-risk-scores',
    icon: ShieldAlert,
  },
];
