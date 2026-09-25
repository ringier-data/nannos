import { useState } from 'react';
import { Bell, Check, CheckCheck, Search } from 'lucide-react';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { formatDistanceToNow } from 'date-fns';
import { toast } from 'sonner';
import {
  getNotificationsApiV1NotificationsGetOptions,
  getNotificationsApiV1NotificationsGetQueryKey,
  getUnreadCountApiV1NotificationsUnreadCountGetOptions,
  markNotificationsAsReadApiV1NotificationsMarkReadPutMutation,
  markAllNotificationsAsReadApiV1NotificationsMarkAllReadPutMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { Separator } from '@/components/ui/separator';

type ApiErrorLike = { detail?: string; response?: { data?: { detail?: string } } };

export function NotificationInbox() {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search);
  const queryClient = useQueryClient();

  const { data: unreadCountData } = useQuery({
    ...getUnreadCountApiV1NotificationsUnreadCountGetOptions({}),
    refetchInterval: 30000, // Poll every 30 seconds
  });

  const { data: notificationsData } = useQuery({
    ...getNotificationsApiV1NotificationsGetOptions({
      query: { limit: 20, unread_only: false, search: debouncedSearch || undefined },
    }),
    enabled: open,
    placeholderData: keepPreviousData,
  });

  const markAsReadMutation = useMutation({
    ...markNotificationsAsReadApiV1NotificationsMarkReadPutMutation(),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: getNotificationsApiV1NotificationsGetQueryKey(),
      });
      queryClient.invalidateQueries({
        queryKey: getUnreadCountApiV1NotificationsUnreadCountGetOptions({}).queryKey,
      });
    },
    onError: (error) => {
      const e = error as ApiErrorLike | null;
      const message = e?.detail || e?.response?.data?.detail || 'Failed to mark notifications as read';
      toast.error(message);
    },
  });

  const markAllAsReadMutation = useMutation({
    ...markAllNotificationsAsReadApiV1NotificationsMarkAllReadPutMutation(),
    onSuccess: () => {
      toast.success('All notifications marked as read');
      queryClient.invalidateQueries({
        queryKey: getNotificationsApiV1NotificationsGetQueryKey(),
      });
      queryClient.invalidateQueries({
        queryKey: getUnreadCountApiV1NotificationsUnreadCountGetOptions({}).queryKey,
      });
    },
    onError: (error) => {
      const e = error as ApiErrorLike | null;
      const message = e?.detail || e?.response?.data?.detail || 'Failed to mark all as read';
      toast.error(message);
    },
  });

  const unreadCount = unreadCountData?.count ?? 0;
  const notifications = notificationsData?.items ?? [];
  const notificationsTotal = notificationsData?.total ?? 0;

  const handleMarkAsRead = (notificationIds: number[]) => {
    markAsReadMutation.mutate({
      body: { notification_ids: notificationIds },
    });
  };

  const handleMarkAllAsRead = () => {
    markAllAsReadMutation.mutate({});
  };

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        // Reopening the inbox should show the latest notifications, not a stale filter.
        if (!next) setSearch('');
      }}
    >
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon" className="relative">
          <Bell className="h-5 w-5" />
          {unreadCount > 0 && (
            <Badge
              variant="destructive"
              className="absolute -top-1 -right-1 h-5 w-5 flex items-center justify-center p-0 text-xs"
            >
              {unreadCount > 99 ? '99+' : unreadCount}
            </Badge>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-96 p-0" align="end">
        <div className="flex items-center justify-between p-4 pb-2">
          <h3 className="font-semibold">Notifications</h3>
          {unreadCount > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={handleMarkAllAsRead}
              disabled={markAllAsReadMutation.isPending}
            >
              <CheckCheck className="h-4 w-4 mr-1" />
              Mark all read
            </Button>
          )}
        </div>
        <div className="px-4 pb-3">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="Search notifications..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 pl-8 text-sm"
            />
          </div>
        </div>
        <Separator />
        <ScrollArea className="h-[400px]">
          {notifications.length === 0 ? (
            <div className="flex flex-col items-center justify-center p-8 text-center text-muted-foreground">
              <Bell className="h-12 w-12 mb-2 opacity-50" />
              <p className="text-sm">{debouncedSearch ? 'No notifications match your search' : 'No notifications'}</p>
            </div>
          ) : (
            <div className="divide-y">
              {notifications.map((notification) => {
                const isUnread = !notification.read_at;
                return (
                  <div
                    key={notification.id}
                    className={`p-4 hover:bg-accent/50 transition-colors ${
                      isUnread ? 'bg-accent/20' : ''
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex-1 space-y-1">
                        <div className="flex items-center gap-2">
                          <p className="font-medium text-sm">{notification.title}</p>
                          {isUnread && (
                            <div className="h-2 w-2 rounded-full bg-primary shrink-0" />
                          )}
                        </div>
                        <p className="text-sm text-muted-foreground">{notification.message}</p>
                        <p className="text-xs text-muted-foreground">
                          {notification.created_at &&
                            formatDistanceToNow(new Date(notification.created_at), { addSuffix: true })}
                        </p>
                      </div>
                      {isUnread && (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8 shrink-0"
                          onClick={() => handleMarkAsRead([notification.id])}
                          disabled={markAsReadMutation.isPending}
                        >
                          <Check className="h-4 w-4" />
                        </Button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </ScrollArea>
        {notificationsTotal > notifications.length && (
          <>
            <Separator />
            <p className="px-4 py-2 text-xs text-muted-foreground">
              Showing the latest {notifications.length} of {notificationsTotal}. Search to find older ones.
            </p>
          </>
        )}
      </PopoverContent>
    </Popover>
  );
}
