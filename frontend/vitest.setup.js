// frontend/vitest.setup.js
//
// Runs once per test file before any test. Adds jest-dom matchers
// (toBeInTheDocument, toHaveTextContent, etc.) so future React component
// tests can use them without reimporting. Hook-only tests don't need
// these, but wiring it once keeps the setup uniform.
import "@testing-library/jest-dom/vitest";
