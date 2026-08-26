import type { SVGProps } from 'react';

/**
 * A SOLID stop square — lucide has no filled variant, and `<SquareIcon
 * className="fill-current"/>` fills the stroked outline, which paints a square
 * that is two stroke-widths too big with a corner radius the stroke inflates.
 * This is a fill-only rect at 14/24 of the box, so it reads with the same
 * weight as the stroked lucide icons beside it.
 *
 * Sized like a lucide icon: `width`/`height` of 24 that any `size-*` class
 * overrides (the Button primitive applies `size-4` to unsized child svgs).
 */
export function StopIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      {...props}
    >
      <rect x="5" y="5" width="14" height="14" rx="3" />
    </svg>
  );
}
