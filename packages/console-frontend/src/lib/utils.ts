import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * A string error body is one the backend did not write as JSON: usually an HTML page from
 * a proxy in front of it (a 502 while it restarts). The page itself is unreadable as a
 * message, so only its title is kept. Nothing is said about retrying: the status is not
 * known here, and a 413 or 403 page will not go away on its own.
 */
export function describeTextError(body: string): string {
  const text = body.trim();
  if (!text.startsWith('<')) return text || 'The request failed with no explanation.';
  const title = new DOMParser().parseFromString(text, 'text/html').title.trim();
  return `The console backend did not answer this request${title ? ` (${title})` : ''}`;
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
    return describeTextError(error);
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
