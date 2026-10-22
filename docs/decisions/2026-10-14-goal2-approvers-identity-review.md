# Decision 2026-10-14: Goal-2 approvers and the identity-review grant

- decided: 2026-10-14 about 02:00Z by the owner
- retires: later_ask `identity-review-policy-grant`
- status: decided; item 2 built (isolated-fixture); item 1 changes no code here

## 1. Who approves Goal-2 actions

The owner and Vince are the Goal-2 approvers. Either one may approve. Once one of them approves an
action through the gate the Workbench already has, an agent may run that action, provided it
is an authorized existing PLX capability. The gate is the per-call approval and the MC gate
decision in `operating/action_path.py`.

The decision widens who approves, not how. Every gate rule stays: the decider is never a
subject (email and oid), the approval binds the exact payload, the roster's action types
apply, and a Graph oid counts only while fresh. An approval given anywhere else (a chat
message, a general "allow everything") authorizes no action.

Today `operating/approver_roster.json` lists Vince only (owner policy item 3).
The owner has no entry yet. A roster entry is keyed by the person's PLX tenant UPN, which a
person must supply; none is invented here. Once it is given, his oid resolves at host
start from the GET-only Graph `/users` read, the same way as Vince's. That roster edit is a
follow-up, as is closing the approver later_ask this item answers.

## 2. Who records identity decisions

The owner and admin employee roles hold `review:identity`
(`operating/ontology_tools_review.py` `IDENTITY_REVIEW`). That principal is the one gate for
`accept_link`, `reject_link`, `alias`, their retractions and an allocation that overrules a
rejection, through `plx.review` and the legacy ledger import.

The Portal has no role called OWNER. Its roles are STAFF, MANAGER, ADMIN and SUPER_ADMIN
(`portal/src/lib/auth/roles.ts`; mirrored in `operating/employee_permissions_portal.py`).
SUPER_ADMIN ("full system access") is the owner tier, ADMIN is the admin. So the grant names
ADMIN and SUPER_ADMIN.

### Options

- A. A role rule in the employee policy (chosen). The policy rule gains `portal_roles`, a list
  of employee roles. The principal is held only when the employee's Portal role is listed
  (legacy mode) and the access level is at least the bootstrap level of a listed role, L4 here
  (canonical mode, which knows levels, not roles). Both must hold, the stricter-of-two rule the
  resolver already applies. A rule without a capability names no Portal resource (its
  `department_ids`, `company_key`, `data_key`, `mrp_modules` and `resource_type` are refused).
  The entry is data in `examples/employee_permission_policy.example.json`, marked
  owner-confirmed.
- B. A capability stand-in, such as `MANAGE_OPERATIONS` or `EDIT_USER_ROLE`. Rejected: every
  capability ADMIN holds above MANAGER is department-scoped, so an admin outside the named
  department would be refused. The global capabilities need L5 and so exclude ADMIN.
- C. A list of people (oids or emails). Rejected: the owner granted roles, and a person list
  is a per-person special case that goes stale when roles change.

### Fails closed

An unknown role, CUSTOMER or SUPPLIER, an empty or non-list `portal_roles`, and a role rule
that also names a resource all refuse the policy file. An ADMIN whose access level is below
L4 and a MANAGER at L5 do not hold the principal. `review:identity` grants no read: no
audience names it, so it only opens the identity-decision gate.

### Evidence

`tests/test_ontology_tools_review.py` `test_the_owner_and_admin_roles_hold_identity_review`
reads the shipped entry. Through the real resolver, ADMIN and SUPER_ADMIN record
`accept_link`, `reject_link` and `alias`. STAFF, MANAGER, MANAGER at L5 and ADMIN at L3 get
`identity_review_required`, and nothing is written. Policy validation is in
`operating/tests/test_employee_permissions.py` `test_policy_validation`. Label:
isolated-fixture; live-unverified.

## What would reverse this

- The Portal gains an OWNER role or an identity-review capability: map the principal to it.
- The owner narrows identity review to a department or names other roles: edit the entry's
  `portal_roles` or add a capability beside it.

## Residual risks

- The deployed policy file is configuration that a person supplies. A host applies the grant
  only once its policy file carries the `review:identity` entry; the example says to copy it
  unchanged.
- Live employee resolution still depends on the Portal access snapshot (docs/reference/permissions.md
  blocker 3). No live identity decision by an owner or admin is recorded yet.
