/**
 * Voice-note capture (ported from the v1 AudioRecorder's MediaRecorder logic):
 * a mic button that toggles recording — red pulsing stop state with elapsed
 * time while capturing; stopping turns the take into a `File` and hands it to
 * `onRecorded` (the composer uploads it like any picked file).
 *
 * Ported behaviors:
 * - Safari/Chrome MIME negotiation (Safari records mp4/wav; Chrome records
 *   webm/ogg — it can DECODE mp4 but cannot ENCODE it via MediaRecorder).
 * - Balanced capture constraints (noiseSuppression OFF to prevent volume
 *   reduction, autoGainControl ON for consistent volume).
 * - `start()` without a timeslice: data accumulates and arrives on `stop()`,
 *   avoiding audio glitches.
 *
 * Renders nothing when the browser cannot record (no `navigator.mediaDevices`
 * / `MediaRecorder`).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MicIcon, SquareIcon } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { cn } from '../../lib/utils';
import { useStrings } from '../../react';
import { useChatEngineOptional } from '../engine';

function canRecord(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    typeof navigator.mediaDevices?.getUserMedia === 'function' &&
    typeof MediaRecorder !== 'undefined'
  );
}

function getSupportedMimeType(): string | null {
  // Safari prefers mp4; Chrome and the rest prefer webm/opus.
  const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
  if (isSafari) {
    for (const type of ['audio/mp4', 'audio/wav']) {
      if (MediaRecorder.isTypeSupported(type)) return type;
    }
  } else {
    for (const type of ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/ogg']) {
      if (MediaRecorder.isTypeSupported(type)) return type;
    }
  }
  // Fallback: any supported type.
  for (const type of ['audio/mp4', 'audio/webm', 'audio/ogg', 'audio/wav']) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return null;
}

function getFileExtension(mimeType: string): string {
  if (mimeType.includes('webm')) return 'webm';
  if (mimeType.includes('ogg')) return 'ogg';
  if (mimeType.includes('mp4')) return 'm4a';
  if (mimeType.includes('wav')) return 'wav';
  return 'audio';
}

function formatTime(totalSeconds: number): string {
  const mins = Math.floor(totalSeconds / 60);
  const secs = totalSeconds % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

export interface AudioRecorderButtonProps {
  /** Receives the finished take as a File (name `recording-<ts>.<ext>`). */
  onRecorded: (file: File) => void;
  disabled?: boolean;
  className?: string;
}

export function AudioRecorderButton({ onRecorded, disabled, className }: AudioRecorderButtonProps) {
  const strings = useStrings();
  const engine = useChatEngineOptional();
  const [isRecording, setIsRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<number | null>(null);
  const startingRef = useRef(false);
  const disposedRef = useRef(false);
  const onRecordedRef = useRef(onRecorded);
  onRecordedRef.current = onRecorded;
  const notify = engine?.adapter.notify;

  const supported = useMemo(canRecord, []);

  const cleanup = useCallback(() => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    recorderRef.current = null;
  }, []);

  // Unmount during a take: drop it silently (the composer is gone anyway).
  useEffect(() => {
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== 'inactive') {
        recorder.onstop = null;
        recorder.stop();
      }
      cleanup();
    };
  }, [cleanup]);

  const start = async () => {
    if (startingRef.current || recorderRef.current) return;
    startingRef.current = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: false, // Disable to prevent volume reduction
          autoGainControl: true, // Enable to maintain consistent volume
        },
      });
      if (disposedRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      streamRef.current = stream;

      const mimeType = getSupportedMimeType();
      if (!mimeType) {
        cleanup();
        notify?.('error', strings['composer.recordError']);
        return;
      }

      const recorder = new MediaRecorder(stream, { mimeType });
      recorderRef.current = recorder;
      chunksRef.current = [];

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: mimeType });
        chunksRef.current = [];
        cleanup();
        if (blob.size > 0) {
          const file = new File([blob], `recording-${Date.now()}.${getFileExtension(mimeType)}`, {
            type: mimeType,
          });
          onRecordedRef.current(file);
        }
      };
      recorder.onerror = () => {
        cleanup();
        setIsRecording(false);
        setElapsed(0);
        notify?.('error', strings['composer.recordError']);
      };

      // No timeslice: MediaRecorder accumulates and delivers the data on
      // stop(), which avoids audio glitches.
      recorder.start();
      setIsRecording(true);
      setElapsed(0);
      if (timerRef.current !== null) clearInterval(timerRef.current);
      timerRef.current = window.setInterval(() => setElapsed((prev) => prev + 1), 1000);
    } catch (err) {
      cleanup();
      const denied =
        err instanceof Error && (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError');
      notify?.('error', denied ? strings['composer.recordPermission'] : strings['composer.recordError']);
    } finally {
      startingRef.current = false;
    }
  };

  const stop = () => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setIsRecording(false);
    setElapsed(0);
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== 'inactive') {
      recorder.stop(); // onstop builds the File and cleans up
    } else {
      cleanup();
    }
  };

  if (!supported) return null;

  const label = isRecording ? strings['composer.recordStop'] : strings['composer.record'];

  return (
    <div data-slot="nannos-audio-recorder" className={cn('flex items-center', className)}>
      {isRecording && (
        <span
          aria-live="polite"
          className="px-1 font-mono text-red-600 text-xs tabular-nums dark:text-red-400"
        >
          {formatTime(elapsed)}
        </span>
      )}
      <Button
        data-slot="nannos-audio-recorder-toggle"
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={label}
        aria-pressed={isRecording}
        disabled={disabled}
        onClick={() => (isRecording ? stop() : void start())}
        className={cn(
          isRecording &&
            'animate-pulse bg-red-500/10 text-red-600 hover:bg-red-500/20 hover:text-red-600 dark:text-red-400 dark:hover:text-red-400',
        )}
      >
        {isRecording ? <SquareIcon className="fill-current" /> : <MicIcon />}
      </Button>
    </div>
  );
}
