# AI use

## Tools

OpenCode, an agent harness, driven by the MiMo model, did the coding: it read the brief and the shipped examples, wrote the harness modules, the eval builder and the tooling, and ran every command in this repo. The harness under test also calls an LLM at runtime (dev endpoint `Atria-Dawn-Preview` via `NOVA_LLM_*`; the grading environment uses its own `ANTHROPIC_*` model) — that runtime use is the product's design, not my development workflow. pymupdf, RapidOCR (dev-only) and the repo's own grader were used as libraries and test oracles.

## How you worked with them

The model drafted each harness layer from the BRIEF, but every dollar figure in every report was recomputed by hand from the extracted page text before I trusted it — the exam overcharges, the rebate with tax and duty stripped out of qualifying spend, the late-fee extension arithmetic, the FX conversion. The grader was run constantly as the oracle (practice card 99.5/100 with all hard gates clear), and a 15-case eval suite was built to pin behavior. Where the model got things wrong: it initially misread the IN contract's term as ending 2031-01-11 based on a stale survey note, which would have mispriced every India invoice — the contract's own clause 2.2 text ("expires on 2032-01-11") settled it. It also produced regexes that silently dropped the exam's duty disbursement rows (date column `-`) and missed register rows keyed `CDU-*` rather than `CDI-*`; both were found by comparing line sums against printed invoice totals, not by the model noticing. Suggestions to memorize period tables or lane rates from the exam were rejected outright — hidden batches only grade unseen documents.

## Written without AI

The hand-verification math and its acceptance thresholds, the eval case designs and their expected amounts (each derived independently of the harness's output), the decision to exclude tax, duty and late fees from qualifying spend per contract clause 1.1, and the selection of boundary dates (60/61-day expiry, quarter-close day) as test axes.

## Prompt log

Development happened inside one long agent session, so there is no separate exported log; the working record is the repo itself — git history, `evals/build_mailboxes.py` (which documents every generated mailbox), the `why:` line in each eval case, and `SOLUTION.md`. Representative prompts and exchanges, in order: parse the BRIEF and examples into a fact/check taxonomy; implement extraction with verbatim citations; implement each check against the practice mailbox until the grader read 99.5; diagnose why the exam's charges-advice layout failed to parse (line-sum audit); verify the India contract's term dates against clause text; hand off expected rebate math and ask for the code to match it, not the reverse; build eval mailboxes from first principles and score them; break the fuel recompute deliberately to prove the suite goes red. No API keys or secrets appear anywhere in this file or the repository.
