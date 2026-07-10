# User Guide — for analysts and end users

This guide is for the people who *use* the validator: MDM analysts working in
the web panel, and SAP end users who attach documents to Change Requests. No
programming knowledge needed. (Developers: see
[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md). Russian operator guide:
[OPERATOR_GUIDE_RU.md](OPERATOR_GUIDE_RU.md).)

## What this tool does

The validator checks the support documents that arrive with vendor-master
requests — **bank confirmation documents** (bank letters, statements, voided
checks, …) and **US W-9 / W-8 tax forms**. It reads the document (including
scans and photos), pulls out the key data (bank account, IBAN, SWIFT, tax ID,
names, signature), checks it against a fixed set of written rules, and gives a
clear verdict with reasons. Everything runs on our own machines — documents
and account numbers are never sent to any cloud AI service. Important to know:
**the AI part only reads the document; it never decides the outcome.** The
outcome comes from explicit rules that a human has reviewed and approved, one
by one.

---

## Part 1 — The web panel (for MDM analysts)

Open the panel in your browser: `https://omen.tail461272.ts.net:8766/ui`
(you need to be on the team's Tailscale network; ask Egor if the page does not
open).

### Checking a document

1. On the **Dashboard**, drag the file into the drop zone (PDF, image, or
   even a whole email `.eml`/`.msg` or `.zip` — the tool finds the actual
   document inside and tells you what it picked).
2. Optionally attach a **screenshot of the SAP Bank Details screen** next to
   it — the tool will then compare the document against SAP character by
   character (see below).
3. Wait for the progress bar (a time estimate is shown — a digital PDF takes
   well under a minute; scans and photos take longer).

### Reading a result

Every check ends in one of **four verdicts**:

| Verdict | Meaning | What you do |
|---|---|---|
| **ACCEPT** | Document is a valid support document and passed all checks | Proceed; use the extracted data |
| **WARNING** | Usable, but something needs attention (e.g. the letter is unsigned) | Read the warning, decide yourself |
| **NEED MANUAL REVIEW** | The tool is not confident, or a check flagged something serious (e.g. IBAN checksum fails, or a rule has not been approved yet) | A human must look at the document before anything is entered in SAP |
| **REJECT** | Not acceptable as a support document at all. Two things always auto-reject: an **invoice** or an **editable file** (Word/Excel/text) offered as bank proof. A **plain email** is currently also rejected, but that is an open question pending the rule owner's decision (see [RULES_AUDIT.md](RULES_AUDIT.md)) | Kick the request back and ask for a proper document (bank letter, bank statement, or supplier letterhead) |

Each result page also shows a **"next step"** line telling you what to do for
that verdict, and every finding names the exact rule that fired (e.g.
`BNK-011 IBAN checksum failed`).

### The extracted data and copy buttons

The run page lists the **EXTRACTED DATA** — every field the tool read, with:

- a tag showing **where the value came from** (read by the model, confirmed
  by exact text recognition, seen in a picture crop, etc.), and often a small
  **evidence thumbnail** — the actual piece of the document the value was
  read from, so you can verify it with your own eyes;
- **copy buttons** so you can paste values straight into SAP, with hints for
  the SAP target field (e.g. W-9 Line 1 → Name 1). What you copy is exactly
  what you see on screen: tax IDs (SSN/EIN/TIN) are **always shown masked and
  copy masked** — the tool never reveals a full tax number anywhere. Bank
  account values are shown in full in this panel (you need the digits to
  verify), but they are never written into any training data.

### The SAP-compare screenshot check

If you attached an SAP Bank Details screenshot (or use "compare with SAP" on
an existing run), the result includes a **comparison table**: document value
vs SAP value, field by field (IBAN, account, SWIFT, bank name, holder, …).
Mismatches become findings — an IBAN mismatch even tells you the position of
the first differing character. This catches typos before they reach the
vendor master.

### "Verify externally" (optional web check)

The run page has a **"Verify externally 🌐"** button. It checks *public*
identifiers (bank routing numbers, SWIFT codes, company names) against
official registries (e.g. FDIC, GLEIF). Two things to know:

- it only runs when you click the button — nothing goes to the internet by
  default, and account numbers / tax IDs are never sent out;
- its results are **informational notes only**. A permanent banner reminds
  you: *the web did not decide this verdict*. Verdicts come only from the
  approved rules.

### When the tool got something wrong — teach it

If a field or the verdict is wrong, press **"Correct — teach the model"** on
the run page. You fix the fields / document type / verdict in a simple form,
optionally tag what kind of failure it was, and submit. What happens then:

- your correction is remembered for **this exact document** immediately — if
  the same file is checked again, your answer wins;
- the correction becomes a (masked) training example. The system retrains a
  **candidate** model in the background and re-checks the document to show
  you a before/after "learning trace";
- a candidate **never replaces the production model by itself** — it must
  pass an automatic quality gate and then be explicitly adopted by the rule
  owner on the Training page (with a one-click rollback if needed).

### When a RULE is wrong — propose a fix

If the extraction was right but the *rule* judged it wrongly, use the
**dispute** button next to the finding (or the free-text "propose a fix" box).
Describe the problem in plain words. The tool drafts a change to the rule file
and shows it as a readable before/after diff — **nothing is applied
automatically**. If the fix needs new program logic, it says so and routes it
to the developer.

### The approvals panel (for the rule owner)

Verdicts can only be produced by rules a human has approved. The panel
`/ui/rules/approve` lists every rule with **Approve ✓ / Reject ✗ / Correct ✎**
buttons:

- while a rule that applies to a document is still *pending*, that document is
  held at **NEED MANUAL REVIEW** — nothing is silently accepted;
- **editing a rule un-approves it** automatically; it must be re-approved
  after any change;
- background for each decision (what every rule does, recommendations) is in
  [RULES_AUDIT.md](RULES_AUDIT.md).

---

## Part 2 — The SAP flow (for end users in Fiori)

If you create vendor/customer **Change Requests** in SAP Fiori, a slimmed-down
check runs inside SAP itself — you do not need the web panel at all. The
in-SAP check does exactly one thing: it deterministically reads the attached
PDF (no AI) and compares its banking data against the values you entered in
the Change Request.

1. Create your Change Request as usual and **attach** the support document
   (the bank letter PDF, the W-9, …) to the request.
2. Press **Check** (or submit — the validation also runs then).
3. Read the **check log**: the validator's findings appear there as
   **warning messages**, with masked values — for example:
   `[SAP-001] IBAN mismatch DE**…4931 vs DE**…4999` means the IBAN typed into
   the request does not match the IBAN in the attached document.

What the messages mean:

- **No validator messages** — either everything matched, or there was nothing
  the in-SAP check could examine (informational all-match notes are not
  shown in the log).
- **Warning (yellow)** — something in the attached document does not match
  what you entered. Warnings never block you from submitting — the in-SAP
  check emits warnings only — but a mismatch warning almost always means a
  typo, so fix the entry or the attachment before submitting.
- Scans, photos, and non-PDF attachments are **not checked in SAP** at all —
  the MDM team checks those in their own web panel instead.

**Who to contact:** if you believe a warning is wrong, or you are unsure what
it asks of you, contact the MDM masterdata team (they own the rules and can
correct them). Technical problems with the check itself go to your SAP support
/ Basis team.

---

## Part 3 — How it gets installed (short version)

You do not install anything yourself. Depending on who you are:

- **MDM analyst (web panel)**: nothing to install — it is a web page. You
  only need access to the team's Tailscale network. The panel runs
  permanently on the team's Mac mini.
- **SAP end user (Fiori flow)**: nothing to install. Ask your **Basis team**
  to import the `ZMDMDOC` abapGit package and follow the go-live checklist —
  the technical document for them is [SAP_READINESS.md](SAP_READINESS.md)
  (statuses of what is done and what must be verified on the system before
  the check is switched on in Fiori).
- **Company IT (running the validator in the data center / cloud)**: two
  prepared variants exist —
  - the **corporate Docker/compose pack**: the full web panel (operator
    console and training pages included) running on a sealed corporate host
    with no internet egress. Technical document: [CORP_DEPLOY.md](CORP_DEPLOY.md).
  - the **BTP / cloud API-only service**: exposes only a locked-down API (no
    training pages, no external web checks, all banking values masked).
    Technical document: [BTP_INTEGRATION.md](BTP_INTEGRATION.md).

### Small glossary

- **Verdict** — the overall result of a check (ACCEPT / WARNING / NEED MANUAL
  REVIEW / REJECT).
- **Finding** — one specific observation, always tied to a named rule
  (`BNK-…` for banking, `W9-…` for tax forms, `SAP-…` for SAP comparison).
- **Rule** — a written, human-approved condition that produces findings. The
  AI never invents rules and never decides verdicts.
- **Masked value** — a value shown with most characters hidden
  (e.g. an EIN shown as `XX-XXX6789`). The console shows account, IBAN, routing
  and tax numbers in full, so you can type them straight into SAP. The
  *Export reasoning* file, the training data and anything sent over the network
  stay masked.
- **Candidate model** — a newly trained version of the reading model that is
  being tested but is not yet in production.
