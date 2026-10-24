import globals from 'globals';
export default [{
  files: ['src/**/*.{js,jsx}', 'scripts/**/*.mjs'],
  languageOptions: { globals: { ...globals.browser, ...globals.node, chrome: 'readonly', __API_BASE_URL__: 'readonly' }, parserOptions: { ecmaFeatures: { jsx: true } } },
  rules: { 'no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^[A-Z]' }], 'no-undef': 'error' },
}];
