import { useState, useRef, useEffect } from 'react';
import { Mic, Square, X, RotateCcw, Check } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

interface AudioRecorderProps {
  onRecordingComplete: (audioBlob: Blob, fileName: string) => void;
  onCancel: () => void;
}

type RecordingState = 'recording' | 'preview' | 'stopped';

export function AudioRecorder({ onRecordingComplete, onCancel }: AudioRecorderProps) {
  const [state, setState] = useState<RecordingState>('recording');
  const [recordingTime, setRecordingTime] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [audioBlob, setAudioBlob] = useState<Blob | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string>('');

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<number | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const isInitializedRef = useRef(false);

  useEffect(() => {
    // Prevent double initialization in React StrictMode
    if (isInitializedRef.current) return;
    isInitializedRef.current = true;
    
    startRecording();
    return () => {
      stopRecording();
      cleanup();
    };
  }, []);

  // Cleanup audio URL on unmount
  useEffect(() => {
    return () => {
      if (audioUrl) {
        URL.revokeObjectURL(audioUrl);
      }
    };
  }, [audioUrl]);

  const cleanup = () => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }
    if (mediaRecorderRef.current) {
      mediaRecorderRef.current = null;
    }
  };

  const startTimer = () => {
    // Clear any existing timer first to prevent multiple intervals
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    timerRef.current = window.setInterval(() => {
      setRecordingTime((prev) => prev + 1);
    }, 1000);
  };

  const stopTimer = () => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  };

  const startRecording = async () => {
    try {
      // Check for browser support
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setError('Audio recording is not supported in your browser');
        return;
      }

      // Request microphone permission with balanced quality settings
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: false, // Disable to prevent volume reduction
          autoGainControl: true,   // Enable to maintain consistent volume
        },
      });
      streamRef.current = stream;

      // Determine supported MIME type
      const mimeType = getSupportedMimeType();
      if (!mimeType) {
        setError('No supported audio format found in your browser');
        stream.getTracks().forEach((track) => track.stop());
        return;
      }

      // Create MediaRecorder
      const mediaRecorder = new MediaRecorder(stream, { mimeType });
      mediaRecorderRef.current = mediaRecorder;
      chunksRef.current = [];

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };

      mediaRecorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: mimeType });
        const fName = `recording-${Date.now()}.${getFileExtension(mimeType)}`;
        const url = URL.createObjectURL(blob);
        
        setAudioBlob(blob);
        setFileName(fName);
        setAudioUrl(url);
        setState('preview');
        // Timer already stopped in stopRecording()
      };

      mediaRecorder.onerror = (event) => {
        console.error('MediaRecorder error:', event);
        setError('Recording error occurred');
        cleanup();
      };

      // Start recording without timeslice to avoid audio glitches
      // MediaRecorder will accumulate data and provide it when stop() is called
      mediaRecorder.start();
      setState('recording');
      startTimer();
    } catch (err) {
      console.error('Error starting recording:', err);
      if (err instanceof Error) {
        if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
          setError('Microphone permission denied. Please allow microphone access to record audio.');
        } else if (err.name === 'NotFoundError') {
          setError('No microphone found. Please connect a microphone and try again.');
        } else {
          setError(`Failed to start recording: ${err.message}`);
        }
      } else {
        setError('Failed to start recording');
      }
      cleanup();
    }
  };

  const stopRecording = () => {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      stopTimer(); // Stop timer immediately, don't wait for async onstop callback
      mediaRecorderRef.current.stop();
      setState('stopped');
    }
  };

  const handleStop = () => {
    stopRecording();
  };

  const handleSend = () => {
    if (audioBlob && fileName) {
      onRecordingComplete(audioBlob, fileName);
      cleanup();
    }
  };

  const handleReRecord = () => {
    // Fully clean up current recording including stream and timer
    stopTimer();
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }
    if (audioUrl) {
      URL.revokeObjectURL(audioUrl);
    }
    setAudioBlob(null);
    setAudioUrl(null);
    setFileName('');
    setRecordingTime(0);
    setState('recording');
    chunksRef.current = [];
    mediaRecorderRef.current = null;
    
    // Start new recording
    startRecording();
  };

  const handleCancel = () => {
    cleanup();
    onCancel();
  };

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const getSupportedMimeType = (): string | null => {
    // Detect browser and use appropriate format
    // Safari: supports mp4 recording and playback
    // Chrome: supports webm recording and playback
    // Note: Chrome can DECODE mp4 but cannot ENCODE it via MediaRecorder
    const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
    
    if (isSafari) {
      // Safari prefers mp4
      const safariTypes = ['audio/mp4', 'audio/wav'];
      for (const type of safariTypes) {
        if (MediaRecorder.isTypeSupported(type)) {
          return type;
        }
      }
    } else {
      // Chrome and other browsers prefer webm
      const chromeTypes = [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/ogg;codecs=opus',
        'audio/ogg',
      ];
      for (const type of chromeTypes) {
        if (MediaRecorder.isTypeSupported(type)) {
          return type;
        }
      }
    }
    
    // Fallback: try any supported type
    const fallbackTypes = ['audio/mp4', 'audio/webm', 'audio/ogg', 'audio/wav'];
    for (const type of fallbackTypes) {
      if (MediaRecorder.isTypeSupported(type)) {
        return type;
      }
    }
    
    return null;
  };

  const getFileExtension = (mimeType: string): string => {
    if (mimeType.includes('webm')) return 'webm';
    if (mimeType.includes('ogg')) return 'ogg';
    if (mimeType.includes('mp4')) return 'm4a';
    if (mimeType.includes('wav')) return 'wav';
    return 'audio';
  };

  if (error) {
    return (
      <div className="flex flex-col items-center gap-4 p-6 bg-card border border-border rounded-lg">
        <div className="text-destructive text-sm text-center">{error}</div>
        <Button onClick={handleCancel} variant="outline" size="sm">
          Close
        </Button>
      </div>
    );
  }

  // Preview state - show audio player and actions
  if (state === 'preview' && audioUrl) {
    return (
      <div className="flex flex-col items-center gap-4 p-6 bg-card border border-border rounded-lg min-w-[320px]">
        <div className="w-full space-y-3">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Mic className="w-4 h-4" />
            <span>Recording complete ({formatTime(recordingTime)})</span>
          </div>
          
          <audio
            ref={audioRef}
            controls
            src={audioUrl}
            className="w-full"
            preload="auto"
          >
            Your browser does not support the audio element.
          </audio>
        </div>

        <div className="flex items-center gap-2 w-full justify-center">
          <Button onClick={handleSend} variant="default" size="sm">
            <Check className="w-4 h-4 mr-2" />
            Send
          </Button>
          <Button onClick={handleReRecord} variant="outline" size="sm">
            <RotateCcw className="w-4 h-4 mr-2" />
            Re-record
          </Button>
          <Button onClick={handleCancel} variant="ghost" size="sm">
            <X className="w-4 h-4 mr-2" />
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  // Recording state

  // Recording state
  return (
    <div className="flex flex-col items-center gap-4 p-6 bg-card border border-border rounded-lg min-w-[280px]">
      <div className="flex items-center gap-3">
        <div
          className={cn(
            'w-12 h-12 rounded-full flex items-center justify-center',
            state === 'recording' ? 'bg-red-500 animate-pulse' : 'bg-muted'
          )}
        >
          <Mic className="w-6 h-6 text-white" />
        </div>
        <div className="text-2xl font-mono font-semibold">{formatTime(recordingTime)}</div>
      </div>

      <div className="flex items-center gap-3 w-full justify-center">
        <Button onClick={handleStop} variant="default" size="sm" disabled={state !== 'recording'}>
          <Square className="w-4 h-4 mr-2" />
          Stop
        </Button>
        <Button onClick={handleCancel} variant="outline" size="sm">
          <X className="w-4 h-4 mr-2" />
          Cancel
        </Button>
      </div>

      {state === 'recording' && (
        <div className="text-xs text-muted-foreground">Recording in progress...</div>
      )}
    </div>
  );
}
