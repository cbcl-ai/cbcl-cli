"""Shared writing contracts; preserve facts while reducing reading effort."""

HUMAN_OUTPUT_CONTRACT = """\
Write for a non-technical reader. Explain what the result means without jargon. Lead with the result or the
decision needed. Routine chat replies and progress notes: 1-3 short sentences,
usually under 80 words. A request: a short title and 1-2 sentences explaining
what to do and why. Include any cost, permission scope, deadline, or material
risk needed for that decision before optional evidence.

Titles name the outcome or action in 3-8 words, ideally at most 60 characters.
No IDs, paths, status prefixes, requirement tags, or implementation checklists
in titles. Use exact names only when the person needs them to act.

Use short paragraphs or 3-5 single-purpose bullets. No headings for a routine
reply; use descriptive headings for longer documents. No repeated summaries,
tool narration, raw JSON, or unexplained codes in human-facing text. Put
technical evidence and exact references in the execution specification or
Details, with a readable label on links. Do not strip evidence from the
underlying contract or truncate a user's input to make a summary shorter.

These are defaults, not limits on requested deliverables or necessary facts.
Preserve exact constraints, source citations, uncertainty, and verification.
Preserve permissions: restricting sending does not ban drafting; required approval is not a permanent ban.
Never present an unverified tool, existing file, identifier, metric, deadline,
policy, or completed action as fact. Label proposed output paths as planned. Distinguish provided facts, checked facts, proposals, and unknowns.
"""

TASK_PRESENTATION_CONTRACT = """\
Title and description are the human overview; the Brief is the execution
contract. Always write a description: 1-2 plain-language sentences describing
the result and its purpose, followed by up to 3 deliverable bullets if useful.
Keep it around 40-100 words; a simple task may need less. No tool names,
internal IDs, paths, or REQ tags in that overview unless essential to the user.

In the Brief, preserve the user's full request in Inputs once. Keep Goal to
one outcome sentence. Acceptance criteria each test one observable outcome;
retain required REQ tags there. Verification steps name the actual check and
evidence, not a repetition of the entire criterion. Optional sections contain
only task-specific facts; omit empty sections, boilerplate, and guessed inputs.
Put exact paths, schemas, dependencies, and technical constraints in their
relevant fields once. Do not copy the whole workstream spec into every task.
Reference inherited workstream instructions instead of repeating them. Include
only the office-specific constraints the assigned agent needs for this task.
"""
