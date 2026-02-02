import { useState } from 'react';
import { Bell, Check, CheckCheck } from 'lucide-react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { formatDistanceToNow } from 'date-fns';
import { toast } from 'sonner';
import {
  getNotificationsApiV1NotificationsGetOptions,
  getUnreadCountApiV1NotificationsUnreadCountGetOptions,
  markNotificationsAsReadApiV1NotificationsMarkReadPutMutation,
  markAllNotificationsAsReadApiV1NotificationsMarkAllReadPutMutation,
} from '@/api/generated/@tanstack/react-query.gen';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { Separator } from '@/components/ui/separator';

export function NotificationInbox() {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();

  const { data: unreadCountData } = useQuery({
    ...getUnreadCountApiV1NotificationsUnreadCountGetOptions({}),
    refetchInterval: 30000, // Poll every 30 seconds
  });

  const { data: notificationsData } = useQuery({
    ...getNotificationsApiV1NotificationsGetOptions({
      query: { limit: 20, unread_only: false },
    }),
    enabled: open,
  });

  const markAsReadMutation = useMutation({
    ...markNotificationsAsReadApiV1NotificationsMarkReadPutMutation(),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: getNotificationsApiV1NotificationsGetOptions({ query: { limit: 20, unread_only: false } }).queryKey,
      });
      queryClient.invalidateQueries({
        queryKey: getUnreadCountApiV1NotificationsUnreadCountGetOptions({}).queryKey,
      });
    },
    onError: (error: any) => {
      const message = error?.detail || error?.response?.data?.detail || 'Failed to mark notifications as read';
      toast.error(message);
    },
  });

  const markAllAsReadMutation = useMutation({
    ...markAllNotificationsAsReadApiV1NotificationsMarkAllReadPutMutation(),
    onSuccess: () => {
      toast.success('All notifications marked as read');
      queryClient.invalidateQueries({
        queryKey: getNotificationsApiV1NotificationsGetOptions({ query: { limit: 20, unread_only: false } }).queryKey,
      });
      queryClient.invalidateQueries({
        queryKey: getUnreadCountApiV1NotificationsUnreadCountGetOptions({}).queryKey,
      });
    },
    onError: (error: any) => {
      const message = error?.detail || error?.response?.data?.detail || 'Failed to mark all as read';
      toast.error(message);
    },
  });

  const unreadCount = unreadCountData?.count ?? 0;
  const notifications = notificationsData?.items ?? [];

  const handleMarkAsRead = (notificationIds: number[]) => {
    markAsReadMutation.mutate({
      body: { notification_ids: notificationIds },
    });
  };

  const handleMarkAllAsRead = () => {
    markAllAsReadMutation.mutate({});
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
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
        <Separator />
        <ScrollArea className="h-[400px]">
          {notifications.length === 0 ? (
            <div className="flex flex-col items-center justify-center p-8 text-center text-muted-foreground">
              <Bell className="h-12 w-12 mb-2 opacity-50" />
              <p className="text-sm">No notifications</p>
            </div>
          ) : (
            <div className="divide-y">
              {notifications.map((notification: any) => {
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
                          {formatDistanceToNow(new Date(notification.created_at), { addSuffix: true })}
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
      </PopoverContent>
    </Popover>
  );
}
