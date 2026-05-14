# Prompt content evals

Content-level regression tests for the AI layer. These complement
`tests/test_worker_prompt.py` (which checks structural assertions like
"the section header exists") with **content** assertions: given a
specific input, the prompt asks Claude to do the right thing — or, just
as importantly, NOT to do the wrong thing.

These are NOT live API calls — they exercise the prompt builders
deterministically and assert on the resulting prompt text. They run on
every CI build with no external dependencies.

## What's covered

| File | Focus |
|------|-------|
| `test_prompt_injection_defenses.py` | XML fences + "treat as data" directives are present in Manager + Worker prompts; literal closer escaping. |
| `test_brief_to_prompt_contract.py` | Required brief fields all surface in the worker prompt; missing brief sections degrade gracefully. |
| `test_review_mode_routing.py` | Designated-reviewer prompt vs non-designated-reviewer prompt vs Manager-Assistant Board-Operator prompt are all distinct and authorise the correct tools. |
| `test_step0_branches.py` | STEP 0 branch selection (fresh / partial-with-activity / artifacts-present / rework) maps correctly to task state. |

## How to add a new eval

1. Build a representative `task_data` or `context_data` dict.
2. Call the prompt builder.
3. Assert on substring presence / absence in the result.

If you find yourself needing to assert on a multi-paragraph block, add a
small helper that finds the section by header and checks within it —
don't make assertions on byte ranges.
