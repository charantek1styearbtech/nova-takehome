# AI use

## Tools

OpenCode, an agent harness, driven by the MiMo model, did the coding: it read the brief and the shipped examples, wrote the harness modules, the eval builder and the tooling, and ran every command in this repo. The harness under test also calls an LLM at runtime (dev endpoint `Atria-Dawn-Preview` via `NOVA_LLM_*`; the grading environment uses its own `ANTHROPIC_*` model) — that runtime use is the product's design, not my development workflow. pymupdf, RapidOCR (dev-only) and the repo's own grader were used as libraries and test oracles.

## How you worked with them

The model drafted each harness layer from the BRIEF, but every dollar figure in every report was recomputed by hand from the extracted page text before I trusted it — the exam overcharges, the rebate with tax and duty stripped out of qualifying spend, the late-fee extension arithmetic, the FX conversion. The grader was run constantly as the oracle (practice card 99.5/100 with all hard gates clear), and a 15-case eval suite was built to pin behavior. Where the model got things wrong: it initially misread the IN contract's term as ending 2031-01-11 based on a stale survey note, which would have mispriced every India invoice — the contract's own clause 2.2 text ("expires on 2032-01-11") settled it. It also produced regexes that silently dropped the exam's duty disbursement rows (date column `-`) and missed register rows keyed `CDU-*` rather than `CDI-*`; both were found by comparing line sums against printed invoice totals, not by the model noticing. Suggestions to memorize period tables or lane rates from the exam were rejected outright — hidden batches only grade unseen documents.

## Written without AI

The hand-verification math and its acceptance thresholds, the eval case designs and their expected amounts (each derived independently of the harness's output), the decision to exclude tax, duty and late fees from qualifying spend per contract clause 1.1, and the selection of boundary dates (60/61-day expiry, quarter-close day) as test axes.

## Prompt log

Verbatim prompts from the OpenCode session record (9–10 Oct 2026), in order. Trimmed only for secrets: the one message containing an endpoint and credential is redacted, greetings and duplicate sends are removed. Responses were the model's; every prompt below is mine.

### Day 1 — scoping and data survey (9 Oct, 17:04–18:01)

- "check the assignment pdf in the folder and explain what is the assignment"
- "what i should build"
- "plan the project"
- "start build"
- "https://github.com/gocomet-india/nova-takehome is this and the one we are building is same"
- "clean the files just keep pdf"
- "https://github.com/gocomet-india/nova-takehome clone this repo to this folder"
- "understand the readme and gloassary and list the check points or feautures we need to have in this project"
- "now let check what kind of test data is present what kind of invoice sources are there and are the formats same are those of multiple companies"

That survey is what produced the multi-format design decision: one row regex per invoice layout instead of one "smart" parser.

### Day 1 — architecture, my calls (9 Oct, 22:18–22:44)

- "now lets think about how we take the input?"
- "how do we plan to take the input sources and make them into universal format?"
- "so we are going to build around orchestrator?"
- "so we are building the only the extraction + unified memory about particular company?"
- "so the analysis part where we need to verify 6 points"
- "plan the whole project and start building if you have any questions ask me and give me summary how did we implement every feature like explain to an non coder"
- "i have another openai compatible key of different model and hybrid and path A only after passing path A try for Path B"
- "i will provide while testing now start building the project"
- *(redacted: API endpoint and key)*

The hybrid rule — deterministic Layer 1/2 pass first, Layer 3 only after, LLM strictly as fallback — is the line above. It stayed the design's spine: see DESIGN.md.

### Day 2 — verification, hardening, write-up (10 Oct)

- "is the test and exam are both passed?"
- "print files structure" / "which we created"
- "remove the cooments from code files and make it look like human written without effecting code"
- "explain the working of the project"
- "how are each layers excuted which algo used which modules used is there any ai invovlement and are there any structure the data stored or schema in each step"
- "take one invoice from each format and show me how the whole thing works while explaining the entire process and technique along with intermediate outputs"
- "more detailed answer"
- "how do i submit"

No API keys or secrets appear anywhere in this file or the repository. The machine's full session export is available on request, and the working record also lives in the repo itself: git history, `evals/build_mailboxes.py`, the `why:` line in each eval case, and `SOLUTION.md`.
