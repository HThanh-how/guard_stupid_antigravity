---
name: change-consequences
description: Check consequences of fixes involving persistence, asset replacement or deletion, rollback, or overlapping asynchronous UI operations. Use during implementation and review of these risky paths; ordinary formatting and unrelated small edits do not require fault-injection.
---

# Verify consequences of a fix

The user's current scope and explicit instructions govern the work. Reuse the existing bounded test-author/reviewer flow; this skill does not create another agent, approval step or retry loop.

Before editing a risky path, trace its real callers and success, failure, cancellation and cleanup branches. Record a short acceptance contract in the task's existing plan/artifact: original user requirements, later corrections, invariants, resource ownership, operation ordering, and the smallest test that would distinguish the proposed fix from the current bug. Preserve this contract through context compaction. A later "continue" does not discard unfinished requirements.

## Persistence and ownership

For create/import/replace/delete/rollback, identify the old resource, newly created resource, persisted references, shared references and the commit point. A failed replacement must preserve the old referenced data. A rollback may delete only resources this attempt newly created and exclusively owns; never infer ownership from a reused identifier or a success-shaped response. Publish new references only after the new resource is durable. Delete the old resource only after replacement is committed and no surviving reference needs it. Do not introduce a new transaction framework when the existing storage primitives can preserve these invariants.

Use disposable fixtures with the existing harness to inject the relevant failures: import failure, reference-save failure, deletion failure, cancellation, or crash boundary where applicable. Assert the final persisted references AND existence/content of old and new resources. Testing only that a promise rejects or delete() was called misses data loss. Test shared/reused assets when supported. Do not probe real user data.

## Overlapping asynchronous work

Identify which request owns each state update, including error, empty-result, fallback and finally branches. Test a slow older success after a newer success, an older failure after a newer success, switching the selected item while pending, and disposal/unmount where relevant. Check that old cleanup does not clear a newer busy/selection/preview state. A guard in one success branch does not protect another branch. Use the established cancellation/request identity pattern rather than arbitrary sleeps or timeout increases.

## Verification and reporting

Send the original acceptance contract, baseline and fault/order scenarios to the independent test author; implementation changes do not authorize weakening expectations. The readonly reviewer must inspect callers, the complete diff, test design, actual execution evidence and coverage of every active user requirement. Distinguish test-design approval before running tests from verified runtime behavior afterward; never require a test result merely to permit its first run.

For each applicable invariant, report the actual scenario, command/log/artifact and observed outcome. Mark missing execution evidence UNVERIFIED; a planned test, an unrelated passing suite, a saved screenshot, or source review alone does not establish runtime safety. Use NOT_APPLICABLE only with a concrete scope reason. Keep unrelated failures separate, do not expand the repair to satisfy generic checks, and do not edit this skill, reviewer instructions, hooks or evidence to obtain PASS unless configuring those files is the user's actual authorized task.

Before a gated Git action, keep that publication step and later remote CI explicitly pending, with evidence that the implementation is ready; do not require a push result before permitting the push. Pending publication cannot excuse missing local verification or invariant evidence.

End with a compact requirement-to-evidence checklist: verified, blocked/unverified, or explicitly superseded by the user. Explain any remaining gap. Respect an explicit instruction to proceed without waiting and report the omitted checks truthfully; never treat a waiver as evidence that a test passed.

When a failing test motivates a production change, establish the product contract from the user's requirements or an existing supported flow before choosing a fix. Do not invent a feature, preset or special case solely to match a fixture. Explain legitimate fixture corrections through independent test ownership. Review current patch layers and directly affected callers; push review additionally covers outgoing commits, while unrelated historical implementations do not need another audit. Preserve original constraints; focus is not permission to silently drop unfinished requirements.

For behavioral fixes, include `flow_verification`: actual entrypoint, relevant caller-to-storage/UI path, observable outcome, discriminating scenarios, inspected sources and executed evidence. An unrelated green suite or a few matching lines cannot establish this flow. Pure moves/formatting can be NOT_APPLICABLE with preserved-behavior evidence. Retain missing local verification after an authorized commit/push; do not treat publication permission as merge approval or CI as a substitute for untested user interactions. Use the separate bounded CI monitor while the implementation owner performs independent useful work.
