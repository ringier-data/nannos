/**
 * The match count a bare-array list endpoint reports in `X-Total-Count`.
 *
 * Endpoints that agents or MCP tools also read keep a bare array body, so the
 * count the console needs for pagination travels in a header instead. The
 * tanstack query wrappers discard the response object, so read it off the
 * generated operation's `response`.
 *
 * Absent when the caller asked for everything, and on any proxy that drops it;
 * the number of rows received is then the honest count.
 */
export function totalCountFrom(response: Response | undefined, received: number): number {
  const header = response?.headers?.get?.('X-Total-Count');
  const total = header != null && header !== '' ? Number(header) : received;
  return Number.isFinite(total) ? total : received;
}
