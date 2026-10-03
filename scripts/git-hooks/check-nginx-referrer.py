#!/usr/bin/env python3
"""nginx / index.html referrer guard (slice 2.5, AC-53).

Usage: check-nginx-referrer.py <nginx.conf> [<nginx.conf> ...] --html <index.html>

No pytest can see these files (the api container mounts ./api only), so the pre-commit hook runs
this against the STAGED content. The SPA document's `location /` must carry
`add_header Referrer-Policy "no-referrer" always;` and, because add_header inheritance is
all-or-nothing, every add_header set at server level must be repeated in that block. index.html
must carry <meta name="referrer" content="no-referrer">. Exits 1, one line per violation.
"""
from __future__ import annotations

import re
import sys


def blocks(text: str) -> list[tuple[str, int, str]]:
    """(header, depth, body-with-nested-blocks-removed) for each brace block; comments stripped."""
    text = re.sub(r"#[^\n]*", "", text)
    out: list[tuple[str, int, str]] = []

    def parse(i: int, depth: int, header: str) -> tuple[int, str]:
        body = ""
        start = i
        while i < len(text):
            c = text[i]
            if c == "{":
                head = re.split(r"[;}]", text[start:i])[-1].strip()
                body += text[start:i][: len(text[start:i]) - len(head)]
                i, _ = parse(i + 1, depth + 1, head)
                start = i
            elif c == "}":
                body += text[start:i]
                out.append((header, depth, body))
                return i + 1, body
            else:
                i += 1
        body += text[start:i]
        out.append((header, depth, body))
        return i, body

    parse(0, 0, "")
    return out


def add_headers(body: str) -> set[str]:
    return {m.group(1).lower() for m in re.finditer(r"^\s*add_header\s+(\S+)", body, re.M)}


def check_conf(path: str) -> list[str]:
    errors: list[str] = []
    found = blocks(open(path, encoding="utf-8").read())
    servers = [b for h, _, b in found if h == "server"]
    spa = [b for h, _, b in found if re.fullmatch(r"location\s+/", h)]
    if not spa:
        return [f"{path}: no `location /` block (the SPA document)."]
    for body in spa:
        if not re.search(r'^\s*add_header\s+Referrer-Policy\s+"?no-referrer"?\s+always\s*;', body, re.M | re.I):
            errors.append(f'{path}: `location /` lacks add_header Referrer-Policy "no-referrer" always;')
        for server in servers:
            for dropped in sorted(add_headers(server) - add_headers(body)):
                errors.append(f"{path}: server-level add_header {dropped} is dropped in `location /` (inheritance is all-or-nothing); repeat it.")
    return errors


def check_html(path: str) -> list[str]:
    html = open(path, encoding="utf-8").read()
    if not re.search(r'<meta\s+name="referrer"\s+content="no-referrer"\s*/?>', html):
        return [f'{path}: missing <meta name="referrer" content="no-referrer">.']
    return []


def main(argv: list[str]) -> list[str]:
    if "--html" not in argv:
        sys.exit("usage: check-nginx-referrer.py <nginx.conf>... --html <index.html>")
    i = argv.index("--html")
    errors: list[str] = []
    for conf in argv[:i]:
        errors += check_conf(conf)
    for html in argv[i + 1 :]:
        errors += check_html(html)
    return errors


if __name__ == "__main__":
    problems = main(sys.argv[1:])
    for line in problems:
        print(f"check-nginx-referrer: {line}", file=sys.stderr)
    sys.exit(1 if problems else 0)
