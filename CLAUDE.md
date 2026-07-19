# Working on Highdeas

Two machines run this app from git checkouts of `main`: Douglas's Windows PC and
his MacBook. Both apps **self-update from origin/main** (pull at launch, pull-and-
relaunch when idle) — anything you land goes live on his desks within minutes.
Land accordingly: whole suite green, small single-purpose commits, no WIP.

**The phone is not one of them.** An iOS app cannot pull from git, so landing an
`ios/` change on `main` reaches the desks and leaves the phone exactly as it was.
It updates only by `ios/resign.sh` with the iPhone plugged in and *unlocked* — the
build is long enough that it auto-locks partway, so unlock it again for the install
step. Until that runs, a phone fix is not fixed, however green the suite is: say so
rather than reporting it landed. And if you need the phone, **ask for it** — Douglas
would rather plug it in than have you verify around its absence on a simulator.

## Land by opening a PR — the gate merges it

**Never merge into the primary checkout, and never push to `main`.** You push your
branch, open a PR, and arm auto-merge. GitHub lands it once
`.github/workflows/merge-gate.yml` is green **and** the branch is up to date with
`main` — `main`'s ruleset requires both — so the run that gates a merge is a run on the
code as it will land, never on a stale base. Landing is still on `origin/main`, so the
desks still self-update from it within minutes of a merge.

*(rtt-python and sagittal-app use a GitHub **merge queue**, which does that rebase for
you and serializes candidates. It is an organization feature: this repo is user-owned,
so the API refuses a `merge_queue` rule. Requiring an up-to-date branch buys the same
property by hand — you rebase, the gate re-runs on that, and it lands. If highdeas ever
moves under an org, add the `merge_queue` rule to the ruleset and put `merge_group:`
back in the workflow's triggers.)*

Work on a branch in a worktree (`git worktree add .claude/worktrees/<name> -b
claude/<name>`), never in the primary checkout. Sync by rebasing onto `origin/main`
on a clean tree; never `reset` to tidy or to sync.

```bash
# from your worktree, on your claude/<name> branch, with your work committed:
git fetch origin && git rebase origin/main    # rebase onto the LATEST main first
git push -u origin HEAD                       # --force-with-lease if the rebase rewrote pushed commits
gh pr create --fill --base main
gh pr merge --auto --rebase                   # arm it; GitHub lands it when the gate is green
```

**Arming auto-merge is not the finish line — landing is.** On a moving `main` a PR
routinely goes `BEHIND` (someone landed first, so your green run no longer describes
what would land) or `DIRTY` (it conflicts), and then sits there forever with auto-merge
armed and never firing. Both are fixed the same way — rebase and force-push — but only
if you are watching for them.

```bash
# Run in the background. Exits — and re-engages you — only when there is something to do:
#   0  merged       → report "PR #N merged" once, then stop
#   10 DIRTY        → conflicts; rebase onto origin/main, resolve inside the rebase, force-push
#   11 BEHIND       → main moved; rebase onto origin/main and force-push (auto-merge stays armed)
#   12 closed       → unexpected; surface to the user
#   13 check failed → read the failing run (`gh run view <id> --log-failed`), fix, push
pr=$(gh pr view --json number -q .number)
while :; do
  st=$(gh pr view "$pr" --json state -q .state)
  [ "$st" = MERGED ] && exit 0
  [ "$st" = CLOSED ] && exit 12
  case "$(gh pr view "$pr" --json mergeStateStatus -q .mergeStateStatus)" in
    DIRTY) exit 10;; BEHIND) exit 11;;
  esac
  # A check that has actually concluded `fail` — not merely pending, which reads BLOCKED.
  if gh pr checks "$pr" 2>/dev/null | grep -qiw fail; then exit 13; fi
  sleep 45
done
```

After a rebase, `git push --force-with-lease`. Auto-merge stays armed, so there is
nothing to re-arm unless the PR was closed.

**Delete your remote branch once the PR is terminal — but only on a positive merge
check.** Deleting the head branch of a still-open PR auto-closes it unmerged, and the
work silently never ships. Never key that on a watcher exit code:

```bash
gh pr view "$pr" --json state,mergedAt -q '.state + " " + (.mergedAt // "null")'
# delete ONLY when this prints "MERGED <timestamp>"
git push origin --delete "$br"
```

Leave the local branch and the worktree alone — you are checked out on them.

**The primary checkout is the running app, and nothing you do should touch it.** Don't
`git checkout`/`switch`/`reset`/`rebase` there or hand-edit it: a stray `git checkout`
detaches the running app's HEAD, which silently swallows every later merge and breaks
the app's own `git pull` self-update. This has already eaten a merge here. Inspect
other branches read-only from your own worktree (`git -C <primary> show/diff/log`).

## Showing the user unlanded work

The desks only ever run `main`, and the user cannot check out your branch — your
worktree holds it. So when they want to *see* a change before it lands, run the app
from your worktree and hand them the URL:

```bash
HIGHDEAS_PORT=<port> <venv-python> run_highdeas.py    # 5200+ ; never 5000, never 5155
```

**5000 is the user's app** (`HIGHDEAS_PORT`'s default) and **5155 is the upload
listener the phone pushes to** — taking either breaks something they are using. Pick a
port in the 5200+ range, one per worktree so parallel sessions don't fight, and kill
only your own port's PID when you are done (`netstat -ano | grep <port>`, then
`taskkill //F //PID <pid>` on Windows — a Git Bash `kill` reaps the wrapper and leaves
the listener up).

## Test agreements

- Python: strict red-green-refactor TDD; `.venv/bin/python -m pytest` — zero
  failures, errors, or skips before every commit.
- Swift pure logic: `cd ios/HighdeasKit && swift test`. The audio/hardware layer
  is verified on the device or simulator, not fake-TDD'd.
- Keep `.env.example`, the README config table, and `pyproject.toml` in sync with
  what the code reads.

## Where the story lives

- `README.md` — what the system is and how each piece is operated.
- `docs/ios-app-handoff.md` — the phone capture app: decisions, wire contract.
- `docs/mac-peer.md` — no-special-machine Highdeas: the shared store, Syncthing,
  fan-out push; decisions and hazards, several learned the hard way.
