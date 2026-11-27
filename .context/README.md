# .context

Handoff notes and decisions for this project, managed by [contextbaton](https://www.npmjs.com/package/contextbaton).

- `handoffs/`: what was done, what's left, and what to watch out for.
- `decisions/`: important decisions and why they were made.

Every note is scanned for secrets before it is saved. Notes can go out of date:
run `npx contextbaton check` to see which ones need checking.

`config.json` sets when a note counts as possibly outdated: more than
`maxChangedLines` changed lines in its related files, or not confirmed for
more than `maxDaysUnconfirmed` days.
