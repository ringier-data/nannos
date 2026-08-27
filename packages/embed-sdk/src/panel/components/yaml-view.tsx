/**
 * Syntax-highlighted YAML `<pre>` for the developer chrome (wire badges, dev
 * inspector). The emitter itself provides the tokens (lib/yaml.ts) — nothing
 * here parses text, so block-scalar prose can never light up as keys. Colors
 * follow the inspector's existing accent conventions and read on `bg-muted`
 * in both themes; block prose stays the pre's own foreground.
 */
import { cn } from '../../lib/utils';
import { toYamlTokens, type YamlTokenType } from '../../lib/yaml';

const tokenClass: Partial<Record<YamlTokenType, string>> = {
  punct: 'text-muted-foreground',
  key: 'text-sky-700 dark:text-sky-400',
  str: 'text-emerald-700 dark:text-emerald-400',
  num: 'text-purple-700 dark:text-purple-400',
  bool: 'text-amber-700 dark:text-amber-500',
  null: 'text-amber-700 dark:text-amber-500',
};

export function YamlView({ value, className }: { value: unknown; className?: string }) {
  const lines = toYamlTokens(value);
  return (
    <pre className={cn('overflow-auto font-mono', className)}>
      {lines.map((line, lineIndex) => (
        // Emitted lines are positional; nothing reorders them — index keys are fine.
        // eslint-disable-next-line react/no-array-index-key
        <span key={lineIndex}>
          {line.map((token, tokenIndex) => {
            const color = tokenClass[token.t];
            if (!color) return token.s;
            return (
              // eslint-disable-next-line react/no-array-index-key
              <span key={tokenIndex} className={color}>
                {token.s}
              </span>
            );
          })}
          {lineIndex < lines.length - 1 && '\n'}
        </span>
      ))}
    </pre>
  );
}
