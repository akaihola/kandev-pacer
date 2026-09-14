# What may the consumer write?

Type: grilling
Status: resolved
Blocked by: 10, 11

## Question

Where is the line between what the consumer writes by itself and what needs Antti's
approval? Decided so far (Antti, 2026-09-13): it writes observations and a proposed
solution prompt into a target repo's "Unverified proposals" registry section, and it
always ignores observations about the consumer task itself so it cannot feed on its
own runs. Immutable records stay untouched.

Still open: on which branch and with what commit the consumer writes into
`kandev-config`, `nixos-config` or a project repo (a direct commit to the default
branch, or a branch plus PR), whether it may create Kandev tasks from a proposal
(proposed: no, a proposal stays unverified until Antti moves it), and how it
recognises its own task (proposed: by the task title template the Heartbeat uses).

## Answer

Decided by Antti, 2026-09-14.

- The consumer may write and commit to the target repository's default branch. No
  branch or PR is needed for a proposal.
- Before writing, the consumer searches the target's proposals for existing ones.
  An exact duplicate is dropped. A similar existing proposal is merged with the new
  one: the ideas of both are combined into a superior new revision that replaces
  the old bullet.
- The consumer always ignores observation records about the consumer task itself,
  so it cannot feed on its own runs.
- Immutable observation records stay untouched.
- Not granted: creating Kandev tasks or backlog items from a proposal. A proposal
  stays under the unreviewed heading until Antti moves it.
