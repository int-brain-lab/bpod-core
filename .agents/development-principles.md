# Development principles

- Keep public APIs backwards compatible unless explicitly requested.
- Prefer small, focused changes; keep diffs minimal and avoid unrelated refactoring.
- State the goal in one sentence first; the smallest change achieving it is the scope.
- Review findings are input, not a to-do list: fix only bugs the change introduces;
  list the rest as follow-ups and ask before widening the scope.
- A simplification pass must shrink the diff.
- Avoid introducing new dependencies without justification.
- Prefer composition over large inheritance hierarchies.
