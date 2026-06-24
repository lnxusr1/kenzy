// Shared Preact + htm binding. Bare specifiers ("preact", "htm") are resolved
// by the <script type="importmap"> in index.html to the vendored ESM files.
import { h } from "preact";
import htm from "htm";

export const html = htm.bind(h);
export { render } from "preact";
export { useState, useEffect, useRef, useCallback } from "preact/hooks";
