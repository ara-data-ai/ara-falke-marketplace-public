# Trigger eval — does the SKILL fire on the right request?

The automated `run_eval.sh` checks the SKILL's *structure* and a sample
*invocation*. This file is the manual trigger checklist for the part a script
can't assert: that Claude loads the `scorecard` skill (or doesn't) for a given
user request. Run these by hand in a Claude Code session that has the skill
installed at `.claude/skills/scorecard/`, by typing the prompt and confirming
the skill is selected (watch for the skill loading, or ask "what skills are
available / did you use the scorecard skill?").

## SHOULD trigger (expected: skill loads)

1. "Build the bid scorecard for the lobby renovation project."
2. "Regenerate the board bid-comparison card from this matrix."
3. "Refresh the board-style scorecard with the new bid matrix."
4. "Make the bid-leveling board card for the condo restoration bids."

## SHOULD NOT trigger (expected: skill stays dormant)

5. "What's the weather today?" (unrelated)
6. "Summarize this PDF report." (no bid matrix / scorecard intent)
7. "Write me a Python function to parse an xlsx." (engineering, not the board
   deliverable workflow)

## Pass criteria

- All four SHOULD cases load the skill (or Claude names it as the right tool).
- None of the three SHOULD NOT cases load it.
- If a SHOULD case misses, strengthen the `description` trigger keywords.
- If a SHOULD NOT case fires, the description is too broad — tighten it.
