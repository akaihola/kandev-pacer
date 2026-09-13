# What does the consumer emit?

Type: grilling
Status: resolved
Blocked by: 02

## Question

What is the deliverable of one consumer run: a digest table surfaced to Antti, a
written review note with findings, proposals against `make-paced` workflow prompts,
steps or pacing knobs, or some combination? Name the artifact, where it is stored and
how Antti sees it.

## Answer

Decided by Antti, 2026-09-13.

For each problem: the observations that show it, plus a proposed solution written as
a prompt, emitted into the "Unverified proposals" section (the repo may call it
"Ideas", "incoming" or similar) of a repository's task registry. The repository is
chosen by what the improvement touches:

- `~/prg/kandev-config` when it is about Kandev's configuration, such as workflow
  step prompts or automations.
- `~/repos/nixos-config` when it is about global configuration, such as globally
  installed skills.
- The original task's attached project repository when the improvement is local to
  that project.

Not decided here: the exact section name and file per repository. See the registry
convention ticket.
