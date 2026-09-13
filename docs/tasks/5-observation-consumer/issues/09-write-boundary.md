# What may the consumer write?

Type: grilling
Status: open
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
