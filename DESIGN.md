# Design

Three layers — extract, understand, analyze — plus a feedback loop. Deterministic first, LLM only where meaning is actually needed, and every fact keeps a verbatim citation to the page it came from.

## Fact model

Every document is stored whole (raw page text preserved) under a stable id like `batch_01/0002.eml#INV-1.pdf`. Extraction produces structured JSON — invoices with line-level rows (description, quantity, rate, amount, date, lane, waybill), contracts with id, term dates, lane/band rate tables, fuel and FX tables, Schedule 1, and named clause quotes — each field carrying the page and verbatim quote it came from. Findings are keyed canonically (`overcharge:INV-ID`, `cashback:CONTRACT/PERIOD/TYPE`, `expiry:CONTRACT`) so grading, learning and later feedback all address the same key. An LLM paraphrase is never used as an amount; numbers come only from deterministic parsing or verbatim quotes.

## Conflicts

The precedence ladder is feedback fact, then signed amendment, then authorised rate notice, then the MSA annex. A conflict exists only when two sources of the same rank cover the same lane and window with different numbers — the practice set pitted amendment against notice, the exam repeated it. When a higher-ranked source answers, it simply wins and the finding cites both winner and loser. On a true conflict we compute the range of defensible amounts from each source (0.00 to 61.93 on the exam), report verdict conflict, and escalate; we never pick a side silently.

## Confidence

Each check starts from a class-specific base — 0.95 when a line reprices exactly against a cited rate, 0.8 for portfolio items — adjusted down when citations are incomplete or authority is indirect (a feedback fact rather than Schedule 1). Practice grading measured Brier 0.0231; AUROC was not computable because nothing was both wrong and right. Conflicts and missing documents cap confidence and force escalation, and autonomous execution requires confidence plus resolvable citations.

## People in the loop

Supervised mode never sends anything outward: findings under USD 5,000 become drafts (dispute, claim credit, request document) and at or above that cap become human review. Conflicts and cannot-determines always escalate to a person. Autonomous mode may execute the same actions only under the USD 5,000 cap with evidence resolving to real documents; a rebate whose credit is not yet due stays internal tracking, and expiry alerts are always internal.

## Feedback

`harness feedback` applies a person's decisions after a batch. Close or reject records the exact finding key as closed — closing with dispute_closed stops raising that key only, so a new invoice with the same wrong rate is still a new finding. Typed facts (rate_accepted, sender_authority, waiver_scope, precedence) become second-level authority from their decided_at date, bounded by valid_from/valid_to and their applies_to filters. One-off scope touches one key; standing scope needs every filter to match. That is what stops a March waiver from silently covering April.

## New documents

Each batch ingests into durable state and every report recomputes from scratch, so an amendment arriving later automatically re-ranks that lane's repricing for pickups after its effective date, latest amendment winning. Amendments attach to their base contract; invoices are attributed to one contract by printed reference, otherwise lane coverage. Unrecognized layouts fall through to the LLM page by page only when the mechanical parse finds nothing — the exam's charges-advice layout and the scanned CDI-3031-0306, via an OCR fallback chain, were both handled that way.

## Cost

LLM-frugal by design: filename and keyword heuristics classify most documents with zero calls; invoices, registers, tracking files and emails all parse deterministically; only ambiguous classification and unrecognized contract pages reach the model. The 86-document exam mailbox ingested in a few minutes locally; extrapolating roughly 1,000 tokens in and 300 out per classified document plus one structured call per contract, a Claude-class model costs on the order of USD 10 to 20 and 15 to 30 minutes per 1,000 documents — ingest-dominated, and partly reusable since state is durable.

## What I'd do next

Unfinished: contract-year credit periods are assumed closed when asked, annual improvement credits parse but no check consumes them, and confidence weights are reasoned defaults rather than fitted numbers. With another week I would add fixture-based unit tests per regex, a layout-templating layer for unfamiliar carriers, a calibration corpus for confidence, parallel batch ingest, and a diff view showing which durable facts changed an earlier answer. Least sure: the confidence weights and the 45-day credit-due convention.
