# 4. Two approval-verification mechanisms, on purpose

**Status:** accepted

When a human approves an action, *how* does the side-effecting component know?
We use two different mechanisms matched to each server's trust boundary:

## Database gateway: stateful verification

The gateway owns the operations database, which already contains the
`proposals` and `approvals` tables. Approval flips `proposals.status` to
'approved'; execution re-reads the exact registered SQL from the table rather
than trusting anything the model says post-hoc. An `applications` row makes
replay idempotent. This is the strong mechanism: what executes is byte-for-byte
what was reviewed.

## GitHub server: HMAC approval tokens

The source-control server is deliberately stateless and talks only to
api.github.com. Rather than give it database access, the API mints a short-TTL
HMAC token binding action, target (`owner/repo#number`) and SHA-256 of the
exact body. `post_comment` verifies the signature locally and fails closed if
no secret is configured. Nothing can redirect the token to a different issue,
and one approved body cannot be swapped for another.

## Why not one mechanism?

Giving the GitHub server DB access couples two services that scale and fail
independently. Minting signed tokens for SQL would mean the executor passes
model-controlled SQL text around again, reopening the gap the proposals table
closed. Each server gets the verification that fits its position in the trust
graph.

Both paths append to the same audit log (`audit.jsonl`, plus structured rows
for approvals), so the review story reads end-to-end regardless of mechanism.
