// @vitest-environment happy-dom
/** `useStateFieldsAdapter`: per-field useState pairs behind the FormLike seam. */
import { act, renderHook } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { useStateFieldsAdapter } from './state-adapter';

function useForm() {
  const [name, setName] = useState('a');
  const [count, setCount] = useState(1);
  const form = useStateFieldsAdapter({ name: [name, setName], count: [count, setCount] });
  return { form, name, count };
}

describe('useStateFieldsAdapter', () => {
  it('reads one field or all mapped fields', () => {
    const { result } = renderHook(useForm);
    expect(result.current.form.getValues('name')).toBe('a');
    expect(result.current.form.getValues()).toEqual({ name: 'a', count: 1 });
  });

  it('writes through the field setter and keeps a stable identity', () => {
    const { result } = renderHook(useForm);
    const first = result.current.form;
    act(() => result.current.form.setValue('count', 5));
    expect(result.current.count).toBe(5);
    expect(result.current.form).toBe(first);
    expect(result.current.form.getValues('count')).toBe(5);
  });

  it('ignores writes to unmapped fields', () => {
    const { result } = renderHook(useForm);
    act(() => result.current.form.setValue('secret', 'x'));
    expect(result.current.form.getValues()).toEqual({ name: 'a', count: 1 });
  });
});
