# Help-Centre FAQ Rules (`docs/faq/faq.md`)

`docs/faq/faq.md` is the user-facing FAQ for Untether. It backs the
marketing-site **FAQPage Schema.org** pipeline shipped in
[`littlebearapps/littlebearapps.com`](https://github.com/littlebearapps/littlebearapps.com)
on `feature/help-seo-geo-items-1-4`. Once the docs-sync mapping (`scripts/docs-sync.config.ts`)
under the `untether` entry references `docs/faq` with `category: faq`,
the marketing site emits `<script type="application/ld+json">` `FAQPage`
JSON-LD on every deploy — unlocking AI-citation surface (ChatGPT,
Perplexity, Google AI Overviews) and SERP rich-snippet eligibility for
the Untether help articles.

Tracking issue: [#477](https://github.com/littlebearapps/untether/issues/477).

## Hard rules

### NEVER delete or move the file

- The path is referenced by the upstream marketing-site sync config.
  Removing it silently breaks the docs-sync mapping (`build-error` per
  the issue's "Coordinated mapping" note) and regresses the FAQPage
  schema on the next deploy.
- The repo enforces this via `.claude/hooks/help-faq-protect.sh`
  (PreToolUse Bash hook). It blocks `rm`, `git rm`, `mv`-away, and
  shell-redirect (`>`) truncation. Append (`>>`) and Edit/Write are
  intentionally NOT blocked — the FAQ is meant to evolve.
- To genuinely retire the FAQ, raise an issue first to coordinate the
  matching mapping removal in `littlebearapps/littlebearapps.com`.

### MUST stay current with feature changes

- Treat the FAQ like a contract with users. Whenever a new feature
  lands in `CHANGELOG.md`, ask: does the existing FAQ still answer
  questions correctly?
- Specifically watch for:
  - **Engine support changes** — Q3 ("Which AI coding agents…")
    enumerates the 6 supported engines. If a new engine lands or one is
    deprecated, update.
  - **Subscription / API key model changes** — Q4 ("Do I need an API
    key?") describes which engines use OAuth vs API key. Any auth-flow
    changes need an FAQ refresh.
  - **Privacy / data flow changes** — Q5 ("Where does my code and data
    go?") covers Telegram, agent CLI, vendor, and Untether itself.
    Any new outbound network call or telemetry MUST be reflected here.
  - **Approval-flow changes** — Q6 ("How do I approve tool calls…")
    documents Plan mode buttons and `/planmode` semantics. Any change
    to ExitPlanMode, ask-mode, or per-engine approval policies needs
    an FAQ pass.
  - **Cost / budget changes** — Q8 ("How do I keep agents from
    spending too much…") shows `[cost_budget]` config. New keys, new
    budget types, or new auto-cancel behaviour need an FAQ refresh.
  - **Voice transcription changes** — Q9 ("Can I send voice notes…")
    references `voice_transcription_*` config keys. Renames or new
    keys need an FAQ pass.
  - **Install / update / uninstall path changes** — Q2/Q10/Q11 cover
    `uv tool` and `pipx` flows. Any change to the wizard, default
    config path, or systemd integration needs an FAQ refresh.

## Soft conventions

### Question shape

- Each `## ` heading MUST be a question. The FAQPage extractor in the
  marketing site only fires on question-shaped H2s — bare topic
  headings like `## Installation` are silently ignored by the schema.
- Phrase as: ends with `?`, OR starts with How / What / Why / When /
  Where / Can / Do / Does / Is / Are / Should / Will.
- Aim for ≥7 H2 Q/A pairs (the issue's acceptance criterion). Currently
  ships with 12. Don't drop below 7 without coordinating with the
  marketing site.

### Answer style

- Each answer is a complete paragraph (or short bullet list with a
  closing sentence). No `TODO`, no `[placeholder]`, no `TBD`.
- Cross-link to existing help-guide URLs (`/tutorials/`, `/how-to/`,
  `/help/`). Broken links degrade the help-centre nav chain — verify
  links resolve before merging.
- Answers should describe **real Untether behaviour**, not aspirational
  features. Source from README, real GitHub Issues, Telegram
  community channels.

### Frontmatter

- Keep frontmatter minimal: `title` + `description` only. The
  marketing-site sync injects `category: faq`, `tool: untether`, and
  dates automatically.
- Don't manually set `category` or `tool` here — that's the sync
  pipeline's job.

## When to update during a release

Suggested cadence as part of the [release-discipline](./release-discipline.md)
workflow:

1. After drafting the CHANGELOG entry for a new release, scan the
   entries against the "MUST stay current" list above.
2. If any FAQ-relevant entry exists, edit `docs/faq/faq.md`
   in-place. Rephrase, add a new Q/A, or update the cross-link.
3. Commit the FAQ touch-up alongside the release commits in the same
   feature branch (don't fragment into a separate PR unless the FAQ
   change is substantial).
4. The marketing-site sync runs nightly and on demand — no manual
   trigger needed once the file is updated.

## After changes

```bash
# 1. Verify shape: ≥7 H2 question-shaped headings, no placeholders
grep -c '^## ' docs/faq/faq.md   # should be ≥ 7
grep -ciE 'TODO|\[placeholder\]|TBD|XXX' docs/faq/faq.md   # should be 0

# 2. Verify each H2 starts with a question word OR ends with ?
grep '^## ' docs/faq/faq.md | \
  grep -vE '^##.*\?$|^## (How|What|Why|When|Where|Can|Do|Does|Is|Are|Should|Will)\b'
# (no output = all H2s are question-shaped)
```
