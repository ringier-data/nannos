import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Search, Wrench, AlertCircle, ChevronDown, ChevronRight, AlertTriangle } from 'lucide-react';
import { playgroundListMcpToolsOptions } from '@/api/generated/@tanstack/react-query.gen';
import type { McpTool, McpToolsResponse } from '@/api/generated/types.gen';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { useAuth } from '@/contexts/AuthContext';

interface MCPToolToggleListProps {
  /** Currently enabled tool names */
  value: string[];
  /** Callback when tool selection changes */
  onChange: (toolNames: string[]) => void;
  /** Whether the component is in a loading state */
  disabled?: boolean;
}

type FilterTab = 'all' | 'enabled' | 'available';

/**
 * Extract parameter information from JSON Schema input_schema
 */
interface ParameterInfo {
  name: string;
  type: string;
  description?: string;
  required: boolean;
}

function extractParameters(tool: McpTool): ParameterInfo[] {
  const schema = tool.input_schema;
  if (!schema || typeof schema !== 'object') return [];

  const properties = schema.properties as Record<string, any> || {};
  const required = (schema.required as string[]) || [];

  return Object.entries(properties).map(([name, prop]) => ({
    name,
    type: prop.type || 'any',
    description: prop.description,
    required: required.includes(name),
  }));
}

/**
 * Format parameter signature (e.g., "required_param, optional_param?")
 */
function formatSignature(params: ParameterInfo[]): string {
  if (params.length === 0) return '()';
  
  const paramStrings = params.map(p => 
    p.required ? p.name : `${p.name}?`
  );
  
  return `(${paramStrings.join(', ')})`;
}

export function MCPToolToggleList({ value = [], onChange, disabled = false }: MCPToolToggleListProps) {
  const { isImpersonating } = useAuth();
  const [activeTab, setActiveTab] = useState<FilterTab>(() => value.length > 0 ? 'enabled' : 'all');
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());

  const { data: mcpToolsData, isLoading, error } = useQuery({
    ...playgroundListMcpToolsOptions({}),
  });

  const allTools = ((mcpToolsData as McpToolsResponse)?.tools ?? []) as McpTool[];
  const enabledSet = useMemo(() => new Set(value), [value]);

  // Filter and search tools
  const filteredTools = useMemo(() => {
    let result = allTools;

    // Apply tab filter
    switch (activeTab) {
      case 'enabled':
        result = result.filter((tool: McpTool) => enabledSet.has(tool.name));
        break;
      case 'available':
        result = result.filter((tool: McpTool) => !enabledSet.has(tool.name));
        break;
      default:
        // all - no filter
        break;
    }

    // Apply search filter
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      result = result.filter(
        (tool: McpTool) =>
          tool.name.toLowerCase().includes(query) ||
          (tool.description?.toLowerCase() || '').includes(query)
      );
    }

    return result;
  }, [allTools, activeTab, enabledSet, searchQuery]);

  const enabledCount = allTools.filter((tool: McpTool) => enabledSet.has(tool.name)).length;
  const availableCount = allTools.length - enabledCount;

  const handleToggle = (toolName: string) => {
    if (disabled) return;

    const newEnabled = new Set(value);
    if (newEnabled.has(toolName)) {
      newEnabled.delete(toolName);
    } else {
      newEnabled.add(toolName);
    }
    onChange(Array.from(newEnabled));
  };

  const toggleSelectAll = () => {
    const allFilteredEnabled = filteredTools.every((tool: McpTool) => enabledSet.has(tool.name));
    
    if (allFilteredEnabled) {
      // Disable all filtered tools
      const filteredNames = new Set(filteredTools.map((tool: McpTool) => tool.name));
      const newEnabled = value.filter((name) => !filteredNames.has(name));
      onChange(newEnabled);
    } else {
      // Enable all filtered tools
      const newEnabled = new Set(value);
      filteredTools.forEach((tool: McpTool) => newEnabled.add(tool.name));
      onChange(Array.from(newEnabled));
    }
  };

  const allSelected = filteredTools.length > 0 && filteredTools.every((tool: McpTool) => enabledSet.has(tool.name));
  const someSelected = filteredTools.some((tool: McpTool) => enabledSet.has(tool.name)) && !allSelected;

  const toggleExpanded = (toolName: string) => {
    const newExpanded = new Set(expandedTools);
    if (newExpanded.has(toolName)) {
      newExpanded.delete(toolName);
    } else {
      newExpanded.add(toolName);
    }
    setExpandedTools(newExpanded);
  };

  // Extract error message from error object
  const getErrorMessage = (err: unknown): string => {
    if (!err) return 'An unknown error occurred';
    
    // Check for API error with detail field
    if (typeof err === 'object' && err !== null) {
      if ('body' in err && typeof err.body === 'object' && err.body !== null && 'detail' in err.body) {
        return String(err.body.detail);
      }
      if ('detail' in err) {
        return String(err.detail);
      }
      if ('message' in err) {
        return String(err.message);
      }
    }
    
    return String(err);
  };

  return (
    <div className="space-y-4">
      {/* Header with tabs */}
      <div className="flex gap-2">
        <Button
          type="button"
          variant={activeTab === 'all' ? 'default' : 'outline'}
          size="sm"
          onClick={() => setActiveTab('all')}
        >
          All ({allTools.length})
        </Button>
        <Button
          type="button"
          variant={activeTab === 'enabled' ? 'default' : 'outline'}
          size="sm"
          onClick={() => setActiveTab('enabled')}
        >
          Enabled ({enabledCount})
        </Button>
        <Button
          type="button"
          variant={activeTab === 'available' ? 'default' : 'outline'}
          size="sm"
          onClick={() => setActiveTab('available')}
        >
          Available ({availableCount})
        </Button>
      </div>

      {/* Search input */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="Search tools by name or description..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="pl-10"
        />
      </div>

      {/* Tools table */}
      {isLoading ? (
        <div className="text-center py-12 text-muted-foreground">
          <div className="inline-block animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full mb-2" />
          <p>Loading MCP tools...</p>
        </div>
      ) : error ? (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Failed to load MCP tools</AlertTitle>
          <AlertDescription>{getErrorMessage(error)}</AlertDescription>
        </Alert>
      ) : allTools.length === 0 && isImpersonating ? (
        <Alert variant="default" className="border-amber-500/50 bg-amber-500/10">
          <AlertTriangle className="h-4 w-4 text-amber-600" />
          <AlertTitle className="text-amber-600">MCP Tools Unavailable During Impersonation</AlertTitle>
          <AlertDescription className="text-amber-600">
            MCP tools require the user's access token which is not available during impersonation.
            Stop impersonating to view and manage MCP tools.
          </AlertDescription>
        </Alert>
      ) : filteredTools.length === 0 ? (
        <div className="text-center py-12 border rounded-lg">
          <Wrench className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
          <p className="text-muted-foreground">
            {searchQuery ? 'No tools match your search' : 'No tools available'}
          </p>
          {searchQuery && (
            <Button type="button" variant="link" onClick={() => setSearchQuery('')} className="mt-2">
              Clear search
            </Button>
          )}
        </div>
      ) : (
        <div className="border rounded-lg">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-12">
                  <Checkbox
                    checked={allSelected}
                    onCheckedChange={toggleSelectAll}
                    aria-label="Select all"
                    disabled={disabled}
                    ref={(el) => {
                      if (el) {
                        (el as any).indeterminate = someSelected && !allSelected;
                      }
                    }}
                  />
                </TableHead>
                <TableHead className="w-12"></TableHead>
                <TableHead className="min-w-[250px]">Tool Name</TableHead>
                <TableHead className="min-w-[300px]">Description</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredTools.map((tool: McpTool) => {
                const isEnabled = enabledSet.has(tool.name);
                const isExpanded = expandedTools.has(tool.name);
                const params = extractParameters(tool);
                const hasParams = params.length > 0;

                return (
                  <>
                    <TableRow key={tool.name} className="group">
                      <TableCell className="align-top">
                        <Checkbox
                          checked={isEnabled}
                          onCheckedChange={() => handleToggle(tool.name)}
                          aria-label={`Enable ${tool.name}`}
                          disabled={disabled}
                        />
                      </TableCell>
                      <TableCell className="align-top">
                        {hasParams && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() => toggleExpanded(tool.name)}
                            className="h-6 w-6 p-0 flex-shrink-0"
                          >
                            {isExpanded ? (
                              <ChevronDown className="h-4 w-4" />
                            ) : (
                              <ChevronRight className="h-4 w-4" />
                            )}
                          </Button>
                        )}
                      </TableCell>
                      <TableCell className="align-top">
                        <div className="flex flex-col gap-1 min-w-0">
                          <div className="flex items-center gap-2 min-w-0">
                            <Wrench className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                            <code className="text-sm font-mono break-all">{tool.name}</code>
                          </div>
                          {hasParams && (
                            <code className="text-xs text-muted-foreground font-mono break-all whitespace-normal">
                              {formatSignature(params)}
                            </code>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-muted-foreground align-top">
                        <p className="whitespace-normal break-words">{tool.description || 'No description available'}</p>
                      </TableCell>
                    </TableRow>
                    
                    {/* Expanded parameter details */}
                    {isExpanded && hasParams && (
                      <TableRow key={`${tool.name}-details`}>
                        <TableCell colSpan={5} className="bg-muted/50 p-4">
                          <div className="space-y-3">
                            <h4 className="text-sm font-semibold">Parameters</h4>
                            <div className="grid gap-3 sm:grid-cols-2">
                              {params.map((param) => (
                                <div
                                  key={param.name}
                                  className="border rounded-lg p-3 bg-background"
                                >
                                  <div className="flex items-start justify-between gap-2 mb-1">
                                    <code className="text-sm font-mono font-semibold">
                                      {param.name}
                                    </code>
                                    <div className="flex gap-1">
                                      <Badge
                                        variant={param.required ? 'default' : 'secondary'}
                                        className="text-xs"
                                      >
                                        {param.required ? 'Required' : 'Optional'}
                                      </Badge>
                                      <Badge variant="outline" className="text-xs">
                                        {param.type}
                                      </Badge>
                                    </div>
                                  </div>
                                  {param.description && (
                                    <p className="text-xs text-muted-foreground mt-2">
                                      {param.description}
                                    </p>
                                  )}
                                </div>
                              ))}
                            </div>
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                  </>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      {/* Results summary */}
      {!isLoading && filteredTools.length > 0 && (
        <div className="text-sm text-muted-foreground">
          Showing {filteredTools.length} of {allTools.length} tools
          {searchQuery && <span> matching "{searchQuery}"</span>}
        </div>
      )}
    </div>
  );
}
