# Contributing to SETT

Thanks for considering contributing: this is an independent, solo
project, and any help is genuinely welcome, at any experience level.

## 🐛 Found a bug, or have an idea?

Open an [Issue](https://github.com/EduDanielV/sett-framework/issues).
Bugs, feature suggestions, questions about the architecture: all of
it belongs there, not lost in a chat somewhere. A good bug report
includes:

- What you ran (the exact command or code snippet)
- What you expected to happen
- What actually happened (the full error/traceback, if there is one)
- Your Python version and OS

## 🔧 Want to fix or improve something yourself?

1. Fork the repository
2. Create a branch for your change
3. Make sure `python -m pytest tests/ -v` passes before opening a PR
4. Open a Pull Request describing what changed and why

If you're adding a new agent/expert pattern or touching `ethics_ruler`,
a quick Issue first (before writing code) is a good idea: it's a
young framework and some design decisions are still being settled.

## Documentation paths

Living contracts and design documents keep one stable, version-independent
path: one concept, one canonical file. Update that file in place and record
its `Introduced` and `Last reviewed` versions inside the document. Git history,
release tags, and `CHANGELOG.md` preserve earlier revisions.

Use a version in a filename only when the document is intentionally tied to a
single release and will not become the current contract, such as a migration
guide or an immutable historical decision record. Do not create a second
versioned copy of an existing living document.

In prose, prefer commas, semicolons, colons, parentheses, or a sentence break
to a dash-shaped pause. Keep hyphens and dashes when they are functional, such
as arithmetic operators, Markdown list markers, changelog dates, numeric
ranges, glossary separators, command options, or tool directives.

## 🧩 New to the codebase?

Start with [`templates/`](templates/): blank, copy-and-fill starting
points for a new agent and a new expert, verified to run as-is before
any customization. `docs/getting_started.md` and `docs/concepts.md`
cover the rest.

## 🌐 Language

Feel free to open Issues or PRs in **English, Español, or 日本語**,
whichever you're most comfortable writing in. I'll answer in kind.

日本語でのフィードバックやIssue報告も大歓迎です。お気軽にご連絡ください。

## What this project is not (yet)

There's no Discord or chat server: Issues are the place for now.
There's no separate translated documentation yet either (this file and
the README are in English only, for the moment). Both are likely to
come later, once there's more to talk about.
