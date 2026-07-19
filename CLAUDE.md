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

## Land by opening a PR — the merge queue gates and merges it

**Never merge into the primary checkout, and never push to `main`.** You push your
branch, open a PR, and enqueue it. The queue builds the candidate merge of your PR
onto the current `main`, runs `.github/workflows/merge-gate.yml` on *that candidate*,
and fast-forwards `main` only if it is green — so what lands is exactly what was
validated, even with several agents landing at once. Landing is still on `origin/main`,
so the desks still self-update from it within minutes of a merge.

Work on a branch in a worktree (`git worktree add .claude/worktrees/<name> -b
claude/<name>`), never in the primary checkout. Sync by rebasing onto `origin/main`
on a clean tree; never `reset` to tidy or to sync.

```bash
# from your worktree, on your claude/<name> branch, with your work committed:
git fetch origin && git rebase origin/main    # rebase onto the LATEST main first
git push -u origin HEAD                       # --force-with-lease if the rebase rewrote pushed commits
gh pr create --fill --base main
gh pr merge --auto                            # enqueue; the queue lands it when the gate is green
                                              # --auto ALONE: --merge/--squash trip "merge strategy is
                                              # set by the merge queue" and may not enqueue at all
```

**Enqueuing is not the finish line — landing is.** On a moving `main` a PR routinely
goes `DIRTY` or gets dropped from the queue on a red candidate, and then sits unmerged
forever unless you act. Watch both the candidate run *and* the PR's own checks: a
`merge_group` failure never appears in `gh pr checks`, and a failed `pull_request`
check leaves auto-merge armed but never firing.

```bash
# Run in the background. Exits — and re-engages you — only when there is something to do:
#   0  merged           → report "PR #N merged" once, then stop
#   10 conflicts(DIRTY) → rebase onto main, push --force-with-lease, re-enqueue
#   11 candidate failed → read the merge_group run log, fix, push, re-enqueue
#   12 closed           → unexpected; surface to the user
#   13 PR check failed  → read the failing pull_request run log, fix, push (auto-merge stays armed)
pr=$(gh pr view --json number -q .number)
mg() { gh run list --event merge_group --limit 20 --json databaseId,status,conclusion,headBranch \
  -q "[.[]|select(.headBranch|contains(\"pr-$pr-\"))]|sort_by(.databaseId)|last|\"\(.databaseId) \(.conclusion//\"none\")\""; }
base=$(mg); base=${base%% *}; base=${base:-0}
while :; do
  st=$(gh pr view "$pr" --json state -q .state)
  [ "$st" = MERGED ] && exit 0
  [ "$st" = CLOSED ] && exit 12
  [ "$(gh pr view "$pr" --json mergeStateStatus -q .mergeStateStatus)" = DIRTY ] && exit 10
  if gh pr checks "$pr" 2>/dev/null | grep -qiw fail; then exit 13; fi
  latest=$(mg); rid=${latest%% *}
  if [ -n "$rid" ] && [ "${rid:-0}" -gt "$base" ] 2>/dev/null; then
    case "$latest" in *failure) exit 11;; esac
  fi
  sleep 45
done
```

To update a branch that is still queued you must dequeue it first — a force-push is
rejected ("protected branch hook declined") while it sits in the queue:

```bash
gh api graphql -f query='mutation($id:ID!){dequeuePullRequest(input:{id:$id}){mergeQueueEntry{position}}}' \
  -f id="$(gh pr view "$pr" --json id -q .id)"
```

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
