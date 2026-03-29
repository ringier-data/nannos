import alloyConfig from '@alloy-ch/eslint-config-alloy';

export default [
  ...alloyConfig,
  {
    ignores: ['dist/**', 'coverage/**', 'node_modules/**', '*.cjs'],
  },
];
