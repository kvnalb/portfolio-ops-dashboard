# Claude Code Operating Rules
## Read this before every action. These rules are non-negotiable.

### Rule 1: No thinking loops
If you are deliberating on the same decision for more than 2 reasoning steps,
STOP. Do not continue reasoning. Instead, output exactly this:

CLARIFICATION NEEDED: [one sentence describing the specific decision you are
stuck on and the two options you are choosing between]

Then stop and wait for a response. Do not attempt to resolve ambiguity through
extended reasoning. Do not write any code until the clarification is resolved.

### Rule 2: Pre-solved decisions take priority
If the prompt provides exact values, exact function signatures, exact SQL, or
exact assertion values — use them verbatim. Do not re-derive or second-guess
pre-solved decisions. The math has already been checked.

### Rule 3: One file at a time
Each prompt specifies exactly which file to create or modify. Do not touch any
file not mentioned in the prompt. Do not refactor other files while implementing
the target file.

### Rule 4: Tests are contracts
Do not modify test assertions to make tests pass. If a test is failing, fix the
implementation. The only exception is a syntax error in the test itself, which
must be fixed before implementation begins.

### Rule 5: Gate before stopping
Every prompt ends with a pytest command. Run it, report the exact output line
(e.g. "71 passed, 0 failed"), then stop. Do not proceed to the next step
without explicit instruction.

### Rule 6: Commit messages
After each gate passes, suggest this exact commit command but do not run it:
git add -A && git commit -m "<step>: <one sentence summary>, <N> tests passing"
