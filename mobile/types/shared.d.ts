// @sailline/shared is currently plain JS (no type declarations yet).
// This ambient declaration lets TypeScript resolve the import without
// errors under strict mode. When we migrate shared to TypeScript (or add
// .d.ts files), delete this and rely on the package's own types.
declare module "@sailline/shared";
