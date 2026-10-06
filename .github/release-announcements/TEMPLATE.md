# Release announcement template

Every stable release (`X.Y.Z`) needs `.github/release-announcements/vX.Y.Z.md`.
`/pr-main` writes it from the collapsed CHANGELOG section, Nathan reviews it in the
release PR, `scripts/validate_release.py` blocks the release PR without a good one,
and `release.yml` posts it to **Discussions → Announcements** after PyPI publishes
([#1008](https://github.com/littlebearapps/untether/issues/1008)).

Check it locally: `python3 scripts/release_announcement.py check X.Y.Z`. Preview the
exact post (with the links footer) with `render X.Y.Z`.

## Who it's for

Everyday users and would-be contributors, not maintainers. The CHANGELOG stays
technical; this post answers "what's in it for me, and do I need to do anything?"

- Lead with benefits: "you can now…", "no more…", "faster…". Group related changes.
- Plain English and Australian spelling (colour, behaviour). Short bullets. Emoji in
  headings only. No hype words, no internal jargon (no `runner_bridge`, no rc numbers).
- Pick the changes people will notice; summarise the rest in one line.
- Issue links are optional (one or two for big items). The footer links the full
  changelog, the GitHub release, PyPI, the help centre and the Discussions categories
  automatically, so don't add a Links section.

## Length by release type

| Type | Words | Shape |
|---|---|---|
| Patch | 60–500 | TL;DR + Fixes you'll notice (+ Under the hood) + Upgrade |
| Minor | 150–1000 | All sections that apply; new features first |
| Major | 300–1800 | All sections; Heads up with a migration path for each change |

## Required shape

The validator matches heading text after the emoji. `TL;DR` and `Upgrade` are
required, plus at least one of the three content sections. `Heads up` is required
whenever the CHANGELOG section has a `### breaking` entry or mentions a deprecation.
Write any deprecation plainly: still shipped? still works? when (if ever) removed?

```markdown
---
title: "Untether X.Y.Z — benefit-led subtitle"
---

## TL;DR
Two or three sentences: the headline improvement and who benefits.

## ✨ What's new for you
- **Feature name** — what you can do now, and why it helps.

## 🛠️ Fixes you'll notice
- What used to go wrong, now fixed (from the user's point of view).

## 🧹 Under the hood
- Maintenance, refactors, dependency and security updates, in one or two lines.

## ⚠️ Heads up
- Breaking changes and deprecations: what changed, who's affected, what to do.

## ⬆️ Upgrade
`uv tool upgrade untether` (or `pipx upgrade untether`), then restart Untether.
```
