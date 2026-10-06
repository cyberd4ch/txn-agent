# Launch publish sheet — copy-paste ready

One-stop sheet: every post below is final text, ready to paste. Posting needs
your logged-in browser sessions (HN/Reddit/LinkedIn have no safe automation
path), so this is the deliverable. Log each post in
[results.md](results.md) when done.

Posting window: **Tue–Thu, 7–9am PT**. Order: HN first, r/AI_Agents same day or
next, r/LocalLLaMA 24h+ later, r/SideProject 24h+ after that, LinkedIn any
morning in the window. Never the same text twice in one day.

Canonical stats (verified 2026-10-06, v0.4.3): **67 tests · 86% coverage
(floor 85 in CI) · ruff + mypy strict clean**. Live local-model verification:
qwen2.5:3b on Ollama, Apple M1 — under-cap purchase completed first try
($64.74); 7b transcript in [architecture.md](../architecture.md) →
*Verification status*.

---

## 0. One-time, before any post

- [ ] Upload `social-preview.png` (1280×640, in repo root, gitignored) —
      GitHub web → repo Settings → Social preview. No REST API exists for
      this; manual only.
- [ ] Confirm the CI/coverage badges render on the repo front page.
- [ ] Skim README "what's mock" so thread answers match it.

## 1. Hacker News — Show HN

Type: **URL post** → `https://github.com/cyberd4ch/txn-agent`
Title (paste exactly):

```
Show HN: txn-agent – a deterministic gate between LLM purchasing agents and money
```

Immediately after posting, add the draft body from
[show-hn.md](show-hn.md) as the **first comment** (URL posts only show text
if it is submitted as a comment). The body in that file is final; paste
verbatim.

Then: reply to every substantive comment within the first 3 hours; concede
the mock-connector objections honestly — the FAQ ([docs/faq.md](../faq.md))
already has the answers.

## 2. Reddit — r/AI_Agents (primary; day 1–2)

Check the sub's self-promo rules and required flair first. Paste the
**r/AI_Agents version** from [reddit.md](reddit.md) verbatim (stats there are
current: 67 tests, 86%).

## 3. Reddit — r/LocalLLaMA (24h+ later)

Paste the **r/LocalLLaMA version** from [reddit.md](reddit.md). Lead with the
model-agnostic angle; include the quick-start so people can run it in one line:

```
ollama pull qwen2.5:7b
python -m txn_agent --llm "restock 2 door gaskets and a drain pump, keep it under 200" \
    --llm-endpoint http://127.0.0.1:11434/v1 --model qwen2.5:7b --budget-ceiling 200
```

Offer to debug setups in the comments; malformed tool output is handled
fail-soft, so broken models produce readable errors instead of crashes —
that is the demo.

## 4. Reddit — r/SideProject (24h+ later)

Paste the **r/SideProject version** from [reddit.md](reddit.md) ("checkout
button's bodyguard" framing, demo-led).

## 5. LinkedIn (Tue–Thu morning)

Personal profile post (not a company page). Paste from
[linkedin.md](linkedin.md) verbatim; link in the body, not preview-only.
As the **first comment**, attach a screenshot of the two terminal transcripts
from [show-hn.md](show-hn.md) — screenshots outperform text on LinkedIn.

## Posting-day micro-checklist

- [ ] HN: first comment up within 5 minutes of the post
- [ ] Reply window 0–3h: every substantive comment answered
- [ ] One row per platform filled in [results.md](results.md) (link + reach)
- [ ] New recurring objections → open a `docs/faq.md` issue the same day
