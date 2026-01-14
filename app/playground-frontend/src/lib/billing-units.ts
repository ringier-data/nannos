/**
 * Utility functions for handling billing units in the usage tracking system.
 * Supports both standard LLM token types and custom billing units.
 */

/**
 * Check if a billing unit name represents an LLM token type.
 * Token types typically end with "_token" or "_tokens" or contain "token" in the name.
 */
export function isTokenType(billingUnitName: string): boolean {
  return billingUnitName.includes('token');
}

/**
 * Categorize a token type as input, output, or other.
 * 
 * Input tokens: base_input_tokens, cache_creation_input_tokens, audio_input_tokens, etc.
 * Output tokens: base_output_tokens, cache_read_input_tokens, reasoning_tokens, audio_output_tokens, etc.
 */
export function categorizeTokenType(tokenType: string): 'input' | 'output' | 'other' {
  const lower = tokenType.toLowerCase();
  
  // Input tokens: base_input_tokens, cache_read_input_tokens, cache_creation_input_tokens, audio_input_tokens
  if (lower.includes('input')) {
    return 'input';
  }
  
  // Output tokens: base_output_tokens, audio_output_tokens, reasoning_output_tokens
  if (lower.includes('output')) {
    return 'output';
  }
  
  return 'other';
}

/**
 * Get a display-friendly label for a billing unit.
 * Converts snake_case to Title Case with special handling for common patterns.
 */
export function getBillingUnitLabel(billingUnitName: string): string {
  // Special cases for better readability
  const specialCases: Record<string, string> = {
    'base_input_tokens': 'Base Input',
    'base_output_tokens': 'Base Output',
    'cache_read_input_tokens': 'Cache Read',
    'cache_creation_input_tokens': 'Cache Creation',
    'reasoning_output_tokens': 'Reasoning',
    'audio_input_tokens': 'Audio Input',
    'audio_output_tokens': 'Audio Output',
  };
  
  if (specialCases[billingUnitName]) {
    return specialCases[billingUnitName];
  }
  
  return billingUnitName
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

/**
 * Get icon/emoji for billing unit type.
 */
export function getBillingUnitIcon(billingUnitName: string): string {
  if (isTokenType(billingUnitName)) {
    return '🔤'; // Token icon
  }
  return '⚙️'; // Custom billing unit icon
}

/**
 * Get color classes for billing unit badge based on type.
 */
export function getBillingUnitColorClass(billingUnitName: string): string {
  if (!isTokenType(billingUnitName)) {
    return 'bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300';
  }
  
  const category = categorizeTokenType(billingUnitName);
  
  switch (category) {
    case 'input':
      return 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300';
    case 'output':
      return 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300';
    default:
      return 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300';
  }
}

/**
 * Group billing units into tokens and custom units.
 */
export function groupBillingUnits(breakdown: Record<string, number>): {
  tokens: Record<string, number>;
  customUnits: Record<string, number>;
} {
  const tokens: Record<string, number> = {};
  const customUnits: Record<string, number> = {};
  
  Object.entries(breakdown).forEach(([name, count]) => {
    if (isTokenType(name)) {
      tokens[name] = count;
    } else {
      customUnits[name] = count;
    }
  });
  
  return { tokens, customUnits };
}
