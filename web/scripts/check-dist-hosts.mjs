// The build's last step (AC-6, ADR-0036): the shipped bundle may not make a browser talk to any
// host but ours. Self-hosting the fonts is a decision; this script is what keeps it one when the
// next person pastes a Google Fonts snippet from a mockup.
//
// Two rules over every dist/**/*.{html,css,js}:
//   (a) nothing the browser fetches by itself — a CSS url()/@import, an HTML <link href> or
//       <script src> — may name a host (http:, https: or protocol-relative //);
//   (b) any other http(s)://host literal must be on ALLOWED below: strings bundled libraries carry
//       that no code ever requests.
// Known ceilings: a protocol-relative //host inside JS is not seen (minified JS is full of `//`),
// and an allow-listed host is trusted to be named, not proven never fetched — the review of each
// ALLOWED entry is the control. A font or CDN snippet arrives through CSS or HTML, which are covered.
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const DIST = fileURLToPath(new URL('../dist/', import.meta.url));

// Every host the current bundle names, and why it is never requested. Add to this list only
// after reading the line that carries the new host.
const ALLOWED = new Map([
  ['www.w3.org', 'SVG/XHTML/MathML namespace URIs in React DOM: identifiers, never fetched'],
  ['react.dev', 'React error-decoder link printed inside a thrown error message'],
  ['reactrouter.com', 'React Router docs link inside an error message'],
  ['github.com', 'React Router: polyfill advice inside a console warning'],
  ['prosemirror.net', 'ProseMirror docs link inside a schema error message'],
  ['tailwindcss.com', "Tailwind's licence banner comment in the CSS"],
  ['localhost', "React Router's dummy base for new URL() when there is no window"],
]);

// Refused by name even if someone adds them to ALLOWED: the one third party ADR-0036 rejects.
const REFUSED = new Set(['fonts.googleapis.com', 'fonts.gstatic.com']);

// Rule (a), by file type: a value that starts with a scheme or `//`, captured up to the host.
// (Scoped, because JS legitimately says `new URL("http://localhost")` — rule (b) judges that.)
const FETCHED = {
  css: [
    /url\(\s*['"]?((?:https?:)?\/\/[^/'")\s]+)/gi,
    /@import\s*(?:url\(\s*)?['"]?((?:https?:)?\/\/[^/'")\s]+)/gi,
  ],
  html: [
    /<(?:link|script|img|source|iframe)\b[^>]*\s(?:href|src|srcset)\s*=\s*['"]?((?:https?:)?\/\/[^/'">\s]+)/gi,
  ],
  js: [],
};
// A host starts with a letter or digit, so our own copy "start with http:// or https://." is not one.
const LITERAL = /https?:\/\/([a-z0-9][a-z0-9.-]*)/gi;

const hostOf = (prefixed) => prefixed.replace(/^(?:https?:)?\/\//i, '').toLowerCase();

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(path);
    else if (/\.(html|css|js)$/i.test(entry.name)) yield path;
  }
}

// An empty dist would pass with nothing checked: a gate that checks nothing must say so.
if (!existsSync(join(DIST, 'index.html'))) {
  console.error(`check-dist-hosts: FAILED — no index.html in ${DIST}; nothing to check`);
  process.exit(1);
}

const failures = [];
const found = new Set();

for (const path of walk(DIST)) {
  const file = relative(DIST, path);
  const text = readFileSync(path, 'utf8');
  for (const pattern of FETCHED[path.split('.').pop().toLowerCase()]) {
    for (const [, prefixed] of text.matchAll(pattern)) {
      failures.push(`${file}: fetches from ${hostOf(prefixed)}`);
    }
  }
  for (const [, raw] of text.matchAll(LITERAL)) {
    const host = raw.toLowerCase();
    found.add(host);
    if (REFUSED.has(host)) failures.push(`${file}: names refused host ${host}`);
    else if (!ALLOWED.has(host)) failures.push(`${file}: names ${host}, not on the allow-list`);
  }
}

const unique = [...new Set(failures)];
if (unique.length > 0) {
  console.error('check-dist-hosts: FAILED — the bundle names a third-party host:');
  for (const line of unique) console.error(`  ${line}`);
  process.exit(1);
}
console.log(`check-dist-hosts: ok — hosts found: ${[...found].sort().join(', ') || 'none'}`);
for (const [host, why] of ALLOWED) console.log(`  allowed ${host}: ${why}`);
