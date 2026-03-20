/**
 * SchedulerNotifications - Listen for scheduler WebSocket notifications and display toasts
 */
import { useEffect, useRef } from 'react';
import { io, Socket } from 'socket.io-client';
import { toast } from 'sonner';
import { Bell, CheckCircle, XCircle, AlertCircle } from 'lucide-react';

interface SchedulerNotification {
  job_id: number;
  job_name: string;
  run_id: number;
  status: string;
  result_summary?: string;
  error_message?: string;
  timestamp: string;
}

export function SchedulerNotifications() {
  const socketRef = useRef<Socket | null>(null);

  useEffect(() => {
    // Create persistent socket connection for scheduler notifications
    const socket = io({ path: '/api/v1/socket.io' });

    socket.on('connect', () => {
      console.log('[SchedulerNotifications] Socket connected');
    });

    socket.on('disconnect', () => {
      console.log('[SchedulerNotifications] Socket disconnected');
    });

    socket.on('scheduler_notification', (data: SchedulerNotification) => {
      console.log('[SchedulerNotifications] Received notification:', data);

      // Show toast based on job status
      const { job_name, status, result_summary, error_message } = data;

      switch (status) {
        case 'success':
          toast.success(job_name, {
            description: result_summary || 'Job completed successfully',
            icon: <CheckCircle className="h-4 w-4" />,
            duration: 5000,
          });
          break;

        case 'failed':
          toast.error(job_name, {
            description: error_message || result_summary || 'Job failed',
            icon: <XCircle className="h-4 w-4" />,
            duration: 7000,
          });
          break;

        case 'condition_not_met':
          toast.info(job_name, {
            description: result_summary || 'Watch condition not met yet',
            icon: <AlertCircle className="h-4 w-4" />,
            duration: 4000,
          });
          break;

        default:
          toast(job_name, {
            description: result_summary || `Status: ${status}`,
            icon: <Bell className="h-4 w-4" />,
            duration: 5000,
          });
      }
    });

    socketRef.current = socket;

    // Cleanup on unmount
    return () => {
      socket.disconnect();
    };
  }, []);

  // This component doesn't render anything - it just maintains the socket connection
  return null;
}
