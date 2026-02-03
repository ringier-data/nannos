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
    const errorObj = error as Record<string, unknown>;
    
    // Try nested response.data.detail (common in axios/fetch wrappers)
    if (errorObj.response && typeof errorObj.response === 'object') {
      const response = errorObj.response as Record<string, unknown>;
      if (response.data && typeof response.data === 'object') {
        const data = response.data as Record<string, unknown>;
        if (typeof data.detail === 'string') {
          return data.detail;
        }
      }
    }
    
    // Try direct data.detail (generated SDK format)
    if (errorObj.data && typeof errorObj.data === 'object') {
      const data = errorObj.data as Record<string, unknown>;
      if (typeof data.detail === 'string') {
        return data.detail;
      }
    }
    
    // FastAPI/backend error format (top-level detail)
    if (typeof errorObj.detail === 'string') {
      return errorObj.detail;
    }
    
    // Generic message field
    if (typeof errorObj.message === 'string') {
      return errorObj.message;
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
