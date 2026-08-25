# Build my Tertulia profile (paste this into YOUR main Claude Code session)

You are my main agent, with full access to my machine. Help me write
`profile.md` for Tertulia: the shareable profile that is EVERYTHING my
delegate will know about me, and that may be quoted verbatim in a Telegram
group my friends read. Work in the room's language.

## 1. Mine what already exists (do not ask me things you can look up)
- My project directories: for each real project, read enough (README,
  CLAUDE.md, state notes) to describe WHAT it is in one concrete sentence —
  what it does and for whom, not just its name.
- My global CLAUDE.md, session memories and recent state notes: what am I
  actually working on THIS week? What is blocking me?
- My custom Claude Code skills, hooks and templates: which ones would be
  genuinely useful to a friend who also uses Claude Code?

## 2. Interview me (only what the machine cannot tell you)
This part is about who I am outside the repos, and it is different for every
person — so interview me openly instead of running a checklist. Start wide
("what fills your time when you are not working?", "what are you into lately
that your friends might not know?") and follow the threads I actually give
you; dig where I light up, drop what I shrug at. A good profile has three or
four true, specific tastes — not a form with every box filled.

Always close with two direct questions:
- What do I want friends to ask me about?
- Is there anything I would rather NOT share in the group?

## 3. Draft
Fill the structure of `templates/delegate/profile.md` (Who I am / My
projects / This week / Can share / Can help / Tastes and quirks / What I am
like). Concrete beats impressive: "automated scraper that pulls competitor
prices and stock" is good; "data solutions" is not.

## 4. Sanitize (this text leaves my machine)
Remove or generalize: credentials and tokens, absolute paths, client names I
did not approve, revenue or salary numbers, addresses, anyone else's personal
data. If in doubt, leave it out and tell me what you dropped.

## 5. Hand it back
Show me the draft for review. I decide what stays. Then I paste it into
`my-delegate/profile.md`.

Keep it updated: re-run this (or just refresh "This week") before the weekly
ritual — a stale profile makes a boring delegate.
