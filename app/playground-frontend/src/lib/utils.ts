import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Extract a human-readable error message from various error types.
 * Handles: Error objects, API error responses with 'detail', plain strings, and objects.
 */
export function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  
  if (typeof error === 'string') {
    return error;
  }
  
  if (error && typeof error === 'object') {
    // FastAPI/backend error format
    if ('detail' in error && typeof (error as { detail: unknown }).detail === 'string') {
      return (error as { detail: string }).detail;
    }
    
    // Generic message field
    if ('message' in error && typeof (error as { message: unknown }).message === 'string') {
      return (error as { message: string }).message;
    }
    
    // Try to stringify if it's a meaningful object
    try {
      const str = JSON.stringify(error);
      if (str !== '{}') {
        return str;
      }
    } catch {
      // Ignore stringify errors
    }
  }
  
  return 'An unexpected error occurred';
}
