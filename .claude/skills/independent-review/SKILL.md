---
name: independent-review
description: Independently review code, documentation, or proposed changes when the user asks for a review, audit, critique, or risk check. Use to identify evidence-backed issues without implementing fixes; do not use when the user asks to build or modify the work.
---

# Independent Review

Review the requested target independently. Treat prior conclusions, agent handoffs, and stated test results as claims to verify rather than evidence to repeat.

## Review the actual target

1. Read the relevant project instructions and the requested files, diff, or commit. Establish the intended behavior and review scope before judging implementation.
2. Inspect the changed behavior and the surrounding code or documentation needed to understand it. Check relevant tests, configuration, and callers when they can change the conclusion.
3. Run read-only checks or tests proportionate to the risk when available. State any verification that could not be performed and why.

## Evaluate issues

Report only issues with a concrete failure mode, regression, safety risk, or material maintenance cost. For each issue, explain:

- the severity and precise location;
- the triggering condition or affected case;
- why the current behavior is harmful; and
- a focused repair direction.

Do not present preferences, hypothetical concerns, or already-resolved behavior as findings. Keep style nits separate from actionable findings.

## Respect review boundaries

Do not modify files, create commits, or implement a fix while reviewing unless the user explicitly asks for that follow-up. A review may inspect and run non-mutating verification, but does not grant authority to change the reviewed work.

## Report format

Lead with findings ordered by severity. If none are found, state that explicitly and note the checks performed and any remaining verification limits.
