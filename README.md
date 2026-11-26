> A note first: I know there are ready-made orchestrators, such as [herdr](https://herdr.dev), and you will most likely want to pick
> one of them. I built this one to understand the process myself.

# depot2

depot2 is a planning and merge depot for a small team that works through outside runners ("executors"): a command
line tool and a read-only web view that keep, in plain files under git, everything a project has in flight — the
themes and their pieces of work, the order they go in, who is working on what, the gates each piece passes, and the
queue that merges the finished pull requests one at a time.

It grew inside one project and was cut out of it: everything that was that project's own (its checks, its deploy,
its paths and machines, its team) now comes from a project config and from plugins, and the depot itself knows
nothing about what it plans. Python 3.8+, the standard library only.

**Status: extracted, not released.** The command line, the reports, the web view and the round prompts speak English
by default; Russian is the second language (`language` in the config, or `$DEPOT2_LANG`; see [Languages](#languages)).
The team of characters is optional: without one, depot2 is the planner and the merge queue. Licensed under the Apache
License 2.0 ([License](#license)).

## Contents

- [Install](#install)
- [Quick start: the demo depot](#quick-start-the-demo-depot)
- [How it works](#how-it-works)
- [Tracks and trains](#tracks-and-trains)
- [The merge queue](#the-merge-queue)
- [Connecting your project](#connecting-your-project)
- [Plugins (hooks)](#plugins-hooks)
- [Executors](#executors)
- [The state's backup](#the-states-backup)
- [JSON formats](#json-formats)
- [Languages](#languages)
- [Glossary](#glossary)
- [Commands](#commands)
- [Tests](#tests)
- [License](#license)

## Install

```sh
git clone <this repository> depot2 && cd depot2
python3 -m depot2 --help          # from the checkout, nothing to install
pip install .                     # or install it: the `depot2` command
```

What it calls, when the commands that need them run: `git` (the depot's own history, the merge queue), `gh` (GitHub:
pull requests, CI runs, repository variables), and whatever your config names (a scan, a smoke check, an executor).

The depot lives in a directory of its own (`--state DIR`, or `$DEPOT2_STATE`; by default
`~/.local/share/depot2/depot`). Nothing is written outside it except where your config points.

## Quick start: the demo depot

```sh
export DEPOT2_STATE=/tmp/depot2-demo
sh examples/demo/make-demo.sh "$DEPOT2_STATE"   # an invented project: two tracks, a train, a stack, queues
python3 -m depot2 board                          # every live car in the column of its stage
python3 -m depot2 trains next                    # what a free slot would take now
python3 -m depot2 stack                          # the executors' slots, the queue, the post-stack
python3 -m depot2 web                            # the same, in a browser, on 127.0.0.1:8765
```

Everything in the demo is made up: the tracks, the cars, the decisions, the names. The script is a plain list of
`depot2` commands, so it doubles as a tour of the command line. A new, empty depot of your own starts with
`depot2 init`.

## How it works

**Roles.** The *owner* decides: their word lets a PR into the merge queue, they answer the open questions, pay out
or fine the team's tokens. The *orchestrator* runs the depot day to day — a person or a main agent that types the
commands, starts the executors' runs and reads the reports. depot2 itself only keeps the books and checks the gates.

**The depot** is a directory of JSON files (the current state of each queue) and append-only JSONL files (every change
of it), committed to the depot's own git after every command that writes. Every write is read back and compared;
nothing deletes a record; one lock serialises the writers. The web view and every `--json` output read the same files.

**Tracks and cars.** A track (`tr-NNNN`) is a theme of the project; a car (`tr-NNNN:N`) is one piece of work on it — a
design document, a pull request, a measurement. A car moves along a route of stages:

    bogie → sketch → project → build → (docs) → acceptance → queued → rolling → arrived
                                                                        (scrapped, with a reason, from anywhere)

A *bogie* is a wish in the owner's words; a *sketch* has a brief; *project* checks it against the code and the other
cars; *build* is an executor's run with a branch and a PR; *acceptance* is the PR waiting for the owner; *queued* is the
merge queue; *rolling* is merged and on its way out; *arrived* is in production. `depot2 car show` prints a car's
history of moves; `depot2 board` (and the web view) shows every live car in the column of its stage.

**Gates.** Moving into the build passes the DoR (definition of ready: the brief exists, no open question, its `after`
is met, the *project* stage passed where required, the brief has the sections of the checks); moving into the
acceptance passes the DoD (definition of done: CI, the heavy test run, the scan, screenshots, the principles' marks,
the docs — each item on or off in `gates.dod`). The gates are soft: `--force --why "…"` passes one and records it.

**The stack** holds the executors' slots (`stack.slots`), a queue of tasks waiting for one (`stack.queue_max`), and the
post-stack: PRs that wait only for the owner's merge and hold no slot (`stack.post_max`). A task in the queue starts in
a freed slot only with `autostart`. Any stack object can carry a lock. The team stack (`tstack`) is a second, lighter
stack for the team rounds.

**Queues** of entries that are not cars yet: wishes (`wl-`), things to think over (`th-`), todos (`td-`). Any entry can
be taken into a car (`take`). Everything — trains, cars, stack objects, entries — has a priority from 1 to 10.

**Principles** (`pc-`) are the project's design rules, kept in the depot and rendered to a Markdown file. The *three
checks* every brief and PR passes are: the owner's words, a check against the code as it is, and a line per principle
in the PR body (`- 4: ok — how`, `- 7: n/a`, `- 2: violated — what instead`; the Russian words of the glossary are read
too). The depot reads those lines into the car's marks: priority 1 principles are gates, the others give the car a
score.

**Decisions** (`dec-`) are the owner's decisions, append-only; `depot2 decisions` reads them with their links.

**Team rounds** (optional) ask a configurable team of characters for their views through capped executor calls:
each character has a token balance, stakes tokens on the points it wants the owner to read first, and gets the stakes
back when the round ends. The ledger, the portraits and the reports are files in the depot. The team comes from the
config's `characters`; null, the default, is no team: the rounds, the tokens, the reflection and the brainstorms
answer «no team configured — set `characters`», and everything else (the stack, the cars, the trains, the gates, the
merge queue, the backup) works the same. `examples/demo/config.json` has a team of five that shows the shape; a
depot's `init` seeds the team its config names.

## Tracks and trains

A **train** is a named run of cars from any tracks (`north-wing`), in one of three states: `design`, `impl` (being
built) and `merge` (in the merge queue), and then `arrived` or `scrapped`. A train has start rules (`after` another
train, `not before` a time, the owner's word), a slot policy (`max N` or `all-free`), notes and a priority. A car is in
at most one live train. `depot2 trains next` answers what a free slot takes now: a repair car out of turn, then the
trains in `impl` by priority, within their slot policies. `depot2 trains --json` and `trains --json next` are the
stable formats the web view reads.

## The merge queue

A PR goes into the queue only on the owner's word, kept verbatim (`depot2 merge add 42 --word "merge"`). One runner
(`depot2 merge run`, its own lock) takes the PRs one at a time, in order, so each is tested on top of the one before:

1. **sync** — the PR's worktree (`merge.worktrees/<branch>`) gets the fresh base branch as a merge commit, made in a
   throwaway worktree; conflicts are solved by a prepared resolution (`merge resolve add`) or by the queue's own rules
   (`merge.auto`: a generated file rebuilt, snapshot files rebuilt by their test, a recorded file re-recorded after the
   push, docs joined when both sides only added text); then `merge.after_resolve`, the scan, the push;
2. **ci** — the PR's CI run on its new head (`merge.ci_workflow`), green;
3. **merge** — `gh pr merge`, the car and its stack object done;
4. **deploy** — the base branch's run of the merge commit, its deploy job (`merge.deploy_job`, or none), the site's
   HTTP status (`merge.site_check`, or none);
5. **smoke** — the main clone fast-forwarded and `merge.smoke` run in it; its verdict comes from the plugins'
   `smoke_verdict`, else from its exit code;
6. **usage** — the plugins' `after_merge` lines for the report.

A failure stops the queue until `merge resume` or `merge drop`. GitHub not starting a job is no failure: the entry
waits («waiting for GitHub») and the runner reruns what GitHub left unstarted. While one PR rides, the next ones are tried on
top of it on branches of their own (`merge.presync_depth`, `merge.trial_prefix`), so their CI runs early. When a
runner pool is offline, `merge.cd_fallback` / `merge.ci_fallback` can move the jobs to a spare pool through a
repository variable and move them back after the run. After a merge the PR's worktree is removed when nothing in it
can be lost; `merge prune-worktrees` takes the old ones.

## Connecting your project

The config is read in layers, each deep-merged over the one before:

1. the defaults (`depot2/config.py`, `DEFAULTS`: every key with a comment);
2. the project's config file, when `$DEPOT2_CONFIG` names one (it can live in the project's repository);
3. the depot's own `config.json`;
4. each `--set key.path=value` of the command line (values parsed as JSON when they parse).

`depot2 config` prints the effective values. The keys a project usually sets:

| Key | Default | What it is |
|---|---|---|
| `project.name`, `project.description` | `example` | the project, in a line, for the team rounds |
| `repo` | null | the project on GitHub, `owner/name` (pull requests, CI, the merge queue) |
| `merge.clone`, `merge.worktrees` | null | the main clone; where a PR's branch has its worktree |
| `merge.remote`, `merge.base` | `origin`, `main` | the remote and the base branch |
| `merge.ci_workflow` | `ci.yml` | the workflow whose runs gate a PR and the merge commit |
| `merge.deploy_job` | null | the job of that workflow that deploys; null: no deploy to wait for |
| `merge.site_check`, `merge.site_ok` | null, `[200]` | a command that prints the site's HTTP status, and the good ones |
| `merge.smoke` | null | the smoke check after the deploy, run in the main clone |
| `merge.scan`, `merge.scan_args` | null, `["{worktree}"]` | the scan before every push: its last line must be exactly `CLEAN`; null refuses to push, `false` pushes without a scan |
| `merge.after_resolve` | `[]` | commands run after a prepared resolution, before the merge commit |
| `merge.auto` | all off | the queue's own conflict rules: `generated`, `snapshots`, `golden`, `union` |
| `merge.cd_fallback`, `merge.ci_fallback` | null | the spare runner pools (see the comment in `config.py`) |
| `plugins` | `[]` | the project's hooks (below) |
| `executor`, `executor_bin`, `model`, `effort` | the bundled adapter, null… | the runner of capped calls and runs (below) |
| `agents.projects` | null | where the executor keeps its runs' logs |
| `stack.slots`, `stack.queue_max`, `stack.post_max` | 4, 10, 4 | the stack's sizes |
| `gates.dor`, `gates.dod` | every item | the gates' items |
| `usage.mtok_per_pct`, `usage.sizes`, `usage.stop_at` | 10, …, 0.9 | the effort unit (below) and the stop for new runs |
| `language` | `en` | the language of every message: `en` or `ru` (`$DEPOT2_LANG` beats it) |
| `characters` | null (no team) | the team of the rounds (`examples/demo/config.json` has one) |
| `deep_repo.clone`, `read_dirs` | null, `[]` | what a team round may read |
| `repair.bugs` | null | a Markdown bug table whose first column is the bug's id |
| `web.hosts`, `web.docs.base`, `web.docs.dirs` | `[]`, null, `[]` | more names of this computer for the web view; where its briefs live |
| `backup.git.*`, `backup.*` | push off until a remote exists | the depot's own backup (below) |

**The effort unit.** Spend is stored in MTok: millions of price-weighted input-equivalent tokens (input ×1, cache
write ×1.25, cache read ×0.1, output ×5). It prints as `X%UW (Y MTok)`: the share of a usage window, at
`usage.mtok_per_pct` MTok per percent. Set it to your runner's own budget; the sizes of cars (`usage.sizes`) are in
`%UW`.

A minimal project config:

```json
{
  "project": {"name": "Acme", "description": "a web shop for garden tools"},
  "repo": "acme/shop",
  "merge": {"clone": "~/src/shop", "worktrees": "~/src/shop-wt", "ci_workflow": "ci.yml",
            "deploy_job": "Deploy", "scan": "~/bin/my-scan",
            "site_check": ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "https://shop.example/"],
            "smoke": ["make", "smoke"]},
  "plugins": ["~/src/shop/tools/depot2_hooks.py"],
  "stack": {"slots": 2}
}
```

## Plugins (hooks)

A plugin is a Python module named in `plugins` (a module on the path, or a path to a `.py` file). It defines any of
these functions; depot2 calls them in the order the plugins are listed (`depot2/hooks.py` has the contract):

| Hook | Called | Returns |
|---|---|---|
| `smoke_verdict(code, out)` | after `merge.smoke` ran (`code` None: it timed out) | `(ok, summary)`, or None to let the next decide |
| `after_merge(cfg, entry)` | the merge queue's last step | `{"line": "…", …}` for the report, or None |
| `snapshot_test(path)` | `merge.auto.snapshots`: which test a snapshot file is | a test path, or None |
| `round_context(cfg, kind)` | building a team round's bundle | `[(title, text), …]`, or None |

`examples/plugins/example_checks.py` is a neutral example of all four. A project's own checks — its smoke output, its
reports after a merge, its file layout — stay in the project, as its plugin.

## Executors

depot2 never runs a model or an agent itself; an executor does. The config's `executor` names a module with three
functions (`depot2/executors/__init__.py` has the interface):

- `run_call(spec, cfg, validate=None)` — one capped call (a team round's pass, a reflection): the prompts, the
  answer's JSON schema, the read-only tools and dirs, the turn and spend caps, the timeout; a result, or `CallFailed`
  (refunded, the round goes on) or `FatalError` (stop; never retried);
- `find_transcript(cfg, run_id)` and `read_transcript(path, tail)` — a run's log, for `agents attach|show|list`.

The runs themselves are started by whoever orchestrates the work: `depot2 agents start <car> --role build --usage N`
asks the gates (the car's stage, the role, the usage window's stop, one live run per car) and gives a slot;
`depot2 agents attach ag-N <run id>` links the run to its log; the depot reads the log when asked and books the spend
on the car once the run is over.

**Who needs one.** Only the team rounds (`round`, with its brainstorms and solo rounds) and `reflect` make calls,
and only `agents attach|show|list` read a run's log. The stack, the cars, the trains, the gates, the merge queue, the
backup and the web view work without an executor; a depot with no team (`characters` null) and no attached runs never
starts one.

**The bundled executor: Claude Code.** `depot2/executors/cli_agent.py`, the default `executor`, runs Claude Code
headless. `executor_bin` is the command that starts it (`claude`; null refuses every call); `model` and `effort` go to
every call as they are (`effort` is left out when null). One capped call is one process in its own process group,
with the user prompt on its stdin:

    claude -p --safe-mode --restricted --strict-mcp-config --disable-slash-commands --no-session-persistence \
      --output-format stream-json --verbose --permission-prompts none [--model M] [--effort E] \
      --system-prompt-file FILE --json-schema SCHEMA --max-budget-usd USD --tools Read,Grep,Glob \
      [--add-dir DIR …] [--disallowedTools RULE …]

- no project instructions, settings, hooks, plugins, MCP servers, skills or slash commands reach the call, nothing is
  saved to resume, and anything that would ask for a permission is denied;
- the system prompt and the answer's JSON schema are depot2's; the tools are read-only (`tools`: Read, Grep, Glob or
  fewer; a reflection gets none), confined to the round's dirs (a snapshot of `deep_repo`, and `read_dirs`) with `deny_read` on top;
- `--max-budget-usd` is the call's cap in MTok × `usage.usd_per_mtok`, since the runner takes its cap in dollars; the
  runner has no turn-cap flag, so depot2 counts the tool turns in the stream and kills the process group at the cap or
  the timeout;
- the child's environment is the caller's without `CLAUDECODE`, any `CLAUDE_CODE_*` variable, `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN`: a call started from inside another session inherits neither that session nor an API key, and
  runs on the runner's own login.

From the event stream depot2 reads the tool uses (for the turn cap), the rate-limit events (the shares of the runner's
usage windows: a round stops before a call that would take a window past `round.stop_at_utilization`) and the final result (its
token usage, its cost and the structured answer). A login, quota or API error, a rejected rate limit, or a runner that
exits without a result is fatal (`FatalError`: the round stops, nothing is retried); the turn cap, the timeout, the
budget cap and an answer that breaks its schema fail only that call (`CallFailed`: refunded, the round goes on).

**Run logs.** `agents.projects` is the folder the runner keeps its sessions in (for Claude Code, `~/.claude/projects`).
`agents attach ag-N <agentId>` finds `<agents.projects>/*/*/subagents/agent-<agentId>.jsonl` (or takes
`--transcript PATH`); `agents show|list` read that file and the `.meta.json` beside it on demand, from where the last
read stopped: the turns, the last tool calls with short arguments, the token usage (weighted into MTok), finished or
stopped. Nothing else in the log is read.

**Another runner**, with `tests/fake_executor.py` as the model:

- a runner that speaks the same stream (one JSON event per line: `rate_limit_event`, `assistant` messages with their
  `tool_use` blocks and `usage`, a final `result` with `usage`, `total_cost_usd` and `structured_output`) needs only
  `executor_bin` pointed at it. `tests/fake_executor.py` is the smallest such runner: it answers the schema with its
  minimal value and has a mode for every failure the adapter tells apart (the tests run every round through it);
- any other runner is a module of its own with the three functions, named in `executor`. `run_call` gets a `CallSpec`
  (`depot2/executors/__init__.py`: the prompts, the schema, the tools, the dirs, the caps in turns, MTok and seconds)
  and returns a dict with `structured` (the answer), `usage` (`input`, `cache_write`, `cache_read`, `output`; or
  `cost_usd` alone), `status`, and `utilization` / `week_utilization` (window shares, 0–1) when the runner reports
  them; it raises `CallFailed` or `FatalError` with that dict. `find_transcript` returns a log's path;
  `read_transcript` returns `{turns, mtok, first_at, last_at, actions, finished, stopped_by_user}`, or None.
  The modes of `tests/fake_executor.py` are the failures such a module has to tell apart.

## The state's backup

The depot can push its own git to any remote you add by name (`git -C <state> remote add backup <url>`;
`backup.git.remote`): after a command, when the remote lacks `backup.git.every_commits` commits, a detached
`depot2 backup --push --auto` pushes them, after the scan of what the push carries and of the files
(`backup.git.scan`: null refuses to push, `false` pushes without a scan). No such remote: nothing happens. A second,
optional way is a daily encrypted archive to a cloud folder through an `rclone` crypt remote defined only in the
child's environment, its keys from `pass` (`backup.remote`, `backup.pass_password`, `backup.pass_salt`; off until
set; `depot2 backup --drive` by hand). Raw executor streams are scanned too before the depot's git takes them; a
stream that is not CLEAN goes to a quarantine outside the depot.

## JSON formats

Every `--json` output is a stable format; the web view reads nothing else:
`board --json` (the columns, their cars and limits), `tracks --json`, `track ID --json`, `trains --json [next]`,
`stack --json`, `merge --json`, `agents --json`, `wishes|think|todo --json`, `principles --json`,
`decisions --json`, `queues --json`. Each is one JSON object; new fields may be added, existing ones keep their
meaning. `merge report --json [--last N]` (the merge queue's tab reads it too) is a list: the last merges' entries.

## Languages

Every text depot2 shows — the command line's messages, the stage names, the reports, the web view and the prompts of
the team rounds — is a key in a message catalog: `depot2/catalogs/en.py` (English, the default and the fallback) and
`depot2/catalogs/ru.py` (Russian). The language of a run is `$DEPOT2_LANG` when it is set, else the config's
`language`, else `en`; one rule for every text. A message is a `str.format` template with named fields; in Russian a
message about one character may have two forms, by the character's grammatical gender (`default_gender`, then
`depot2 gender`), which no other language reads. The words depot2 reads — a stage typed by name, yes and no, the
principles section of a PR body, the sections of a brief — are accepted in every language at once (the catalogs'
`in.` keys), so a depot reads what was written in either. `tests/test_i18n.py` checks that both catalogs hold the
same keys with the same fields and that no Russian text is left outside `ru.py`. A new language is one more catalog
with the same keys, and its name in `depot2/i18n.py` `LANGS`.

## Glossary

The command line's words in both languages, with the keys the data use:

| Key | English | Russian | Meaning |
|---|---|---|---|
| `bogie` | bogie | тележка | a wish, no brief yet |
| `sketch` | sketch | эскиз | a brief: what, why, the frame, the size, open questions |
| `project` | project | проект | checked against the code, the other cars and the principles |
| `build` | build | сборка | an executor's run, a branch, a PR |
| `docs` | docs | документация | the docs of the change (optional) |
| `acceptance` | acceptance | приёмка | the PR waits for the owner (the post-stack) |
| `queued` | queued | на путях | in the merge queue |
| `rolling` | rolling | в пути | merged, rolling out |
| `arrived` | arrived | прибыл | in production |
| `scrapped` | scrapped | списан | given up, with a reason |
| `design`, `impl` | design, implementation | дизайн, имплементация | a train's states before the merge queue |
| — | defect / claim / repair | брак / рекламация / ремонт | a car sent back after hand-in / a defect found in production / the car that repairs it |
| — | the owner's word | слово владельца | the word that lets a PR into the merge queue |
| — | gates, the car's score | ворота, балл вагона | priority 1 principles (gates), the others (the car's score) |
| `ok`, `na`, `bad` | ok, n/a, violated | ок, не касается, нарушен | a principle's mark in a PR body |
| — | «Check against the code», «Principles check» | «Сверка с кодом», «Проверка по принципам» | the sections of the three checks in a brief and a PR body |

## Commands

Every command and subcommand has `--help`. Aliases: `wish` = `wishes`, `agent` = `agents`, `train` = `trains` (one
train), `track` = `tracks` (one track), `principle` = `principles`.

```
depot2 round                       # run a team round
depot2 report                      # rebuild a round's report.md from its saved data (no model call)
depot2 payout                      # record the owner's payout
depot2 fine                        # the owner's fine: take n tokens (never below zero)
depot2 refund                      # refund open stakes of an older round (the owner's call); --auto: return them as …
depot2 forfeit                     # close open stakes of an older round the owner does NOT return
depot2 brainstorm                  # the team's own brainstorms: list, or cancel one that never ran
depot2 brainstorm list             # the approved ones (--all: done, failed, cancelled too)
depot2 brainstorm cancel           # it never ran: every fee comes back (brainstorm_refund)
depot2 orders                      # the queue of measurement and test-run orders
depot2 order                       # an order: done (the orchestrator ran it) or settle (the owner decides)
depot2 order done                  # record the results (copied into orders.results_dir/<id>/)
depot2 order settle                # moved = the price back + 1; burn = nothing; refund = the price back
depot2 stack                       # the agents' slots, the post-stack and the queue (stack.json); no action: print it
depot2 stack add                   # into the queue
depot2 stack start                 # queue → slot (whatever its autostart says; a parked object comes back); …
depot2 stack post                  # slot → the post-stack: its PR is ready (CI green) and waits only for the owner's …
depot2 stack set                   # change an object's fields
depot2 stack move                  # reorder the queue
depot2 stack take                  # an object that is no car becomes a new car of the track, and stays in its place …
depot2 stack done                  # merged, closed or dropped: it leaves the stack (a track's car's object: --car is …
depot2 stack park                  # a PR that waits frees its slot: a car's object goes back to its track (status …
depot2 stack unlock                # lift a lock: --met (the condition landed) or --owner
depot2 ballots                     # the owner's ballot queue (ballots.json); no action: print it
depot2 ballots add                 # a round that already exists
depot2 ballots point               # mark a point answered
depot2 ballots at                  # where the presentation stopped
depot2 ballots move                # reorder the queue
depot2 ballots done                # the owner answered it
depot2 ballots drop                # dropped without an answer
depot2 tstack                      # the team stack (team-stack.json): every interaction with the team; no action: …
depot2 tstack add                  # into the team queue
depot2 tstack start                # queue → team slot (prints the command that runs it)
depot2 tstack set                  # change an item's fields
depot2 tstack move                 # reorder the team queue
depot2 tstack done                 # it leaves the team stack
depot2 tstack take                 # an item of the team queue becomes a new car of the track (a bogie, from: …
depot2 tracks                      # the tracks (tracks.json): the themes and their cars; no action: print them
depot2 tracks add                  # a new track (a theme)
depot2 tracks set                  # change a track
depot2 tracks import               # one-off: the tracks from a JSON the orchestrator writes
depot2 tracks car                  # a track's cars: add, set, move, stack, check
depot2 tracks car add              # a new car at the end (or --at N)
depot2 tracks car set              # change a car (tr-NNNN:N)
depot2 tracks car move             # its place on the track (--at K); or to another track (--to tr-NNNN) or name (--n …
depot2 tracks car stack            # put the car into the agents' stack (linked); --start: into a free slot now (a …
depot2 tracks car check            # its marks by the principles: print them; --from-pr reads its PR's «Principles check …
depot2 track                       # one track: its cars
depot2 trains                      # the trains (trains.json): named runs of cars — design, implementation, …
depot2 trains add                  # a new train, in «design»
depot2 trains set                  # change a train
depot2 trains state                # move it: design|impl|merge — forward through its gate, back (merge → impl → …
depot2 trains done                 # every car is merged or dropped: it arrived
depot2 trains drop                 # scrap it: its open cars stay on their tracks
depot2 trains next                 # what a free slot takes now: a repair out of turn, then the trains in …
depot2 trains car                  # a train's cars: add, move, drop (the car stays on its track)
depot2 trains car add              # a car tr-NNNN:N at the end (or --at N)
depot2 trains car move             # its place in the train
depot2 trains car drop             # out of the train
depot2 train                       # one train: its cars in order
depot2 migrate                     # bring the depot's data to today's form, once (schema 1 → 5: tracks and trains, …
depot2 priority                    # the priority 1–10 (10 — the most important, default 5) of a train or a car in …
depot2 car                         # one car (tr-NNNN:N, st-, wl-, th-): its stage, show, its open questions, the …
depot2 car stage                   # move its stage, on the route; the moves a command makes are refused, each with …
depot2 car show                    # its stage, where it may go, the next gate, its history
depot2 car ask                     # an open question: the DoR waits for its answer
depot2 car answer                  # close an open question with its answer
depot2 car needs-project           # the stage «project» is required (the data model, the server, privacy and policy; …
depot2 board                       # the board: every live car in the column of its stage (Kanban), the columns' …
depot2 repair                      # defects and claims: a claim on a car on production and its repair car (repair add …
depot2 repair add                  # a claim: a new repair car (kind repair) at the head of the stack's queue, …
depot2 repair link                 # the culprit of a repair, found later; or a defect repaired inside another car …
depot2 claims                      # defects and claims: which tracks and cars gave them, for a period
depot2 agents                      # the agents' runs (agents.json): the gates before the orchestrator starts one, …
depot2 agents start                # the gates (the role and the car's stage, the usage window's stop, one live agent …
depot2 agents attach               # link it to its transcript: found by the agentId under agents.projects (the …
depot2 agents show                 # one agent: its fields and its last actions
depot2 agents stop                 # record its stop (the orchestrator stops the process): its spend so far on the car
depot2 web                         # depot2's web view, seven tabs: tracks, trains, board, merge queue, agents, …
depot2 wishes                      # the owner's wishes not scheduled yet (wishes.json); no action: print it
depot2 wishes add                  # a new open entry (its id never reused)
depot2 wishes set                  # change an open entry's fields (--note, --link, --question, --where append)
depot2 wishes show                 # every field of one entry
depot2 wishes take                 # it becomes a track's car and closes «→ tr-NNNN:N»: --to tr-NNNN a new bogie (its …
depot2 wishes drop                 # declined
depot2 wishes done                 # «done»: it came true (not drop); off the open table, in --all
depot2 think                       # open questions for a brainstorm, a round or the owner (think.json); no action: …
depot2 think add                   # a new open entry (its id never reused)
depot2 think set                   # change an open entry's fields (--note, --link, --question, --where append)
depot2 think show                  # every field of one entry
depot2 think take                  # it becomes a track's car and closes «→ tr-NNNN:N»: --to tr-NNNN a new bogie (its …
depot2 think round                 # link it to a team round (a round id or a tm-NNNN)
depot2 think decide                # decided: --dec dec-NNNNNN and/or --reason
depot2 think drop                  # no longer a question
depot2 todo                        # leftovers and reminders (todo.json): date, version, place; no action: print it
depot2 todo add                    # a new open entry (its id never reused)
depot2 todo set                    # change an open entry's fields (--note, --link, --question, --where append)
depot2 todo show                   # every field of one entry
depot2 todo take                   # it becomes a track's car and closes «→ tr-NNNN:N»: --to tr-NNNN a new bogie (its …
depot2 todo pr                     # a PR takes it
depot2 todo done                   # done (several ids at once)
depot2 todo wont                   # we won't (several ids at once)
depot2 principles                  # the design principles (principles.json): PRINCIPLES.md is rendered from them, …
depot2 principles add              # a new principle: the next number (never reused)
depot2 principles set              # change one: fields replaced (the history keeps them), --words and --sub appended
depot2 principles show             # every field of one principle, and its lines in the file
depot2 principles replace          # replaced by another one (out of the file, in --all)
depot2 principles drop             # no longer a principle (out of the file, in --all)
depot2 principles head             # the file's head (before the first section); --text replaces its text under the …
depot2 principles import           # one-off: today's PRINCIPLES.md, without loss (then it is rendered, read-only)
depot2 principles render           # write PRINCIPLES.md from the depot (read-only between renders)
depot2 principles checks           # the three checks: the owner's words, «Check against the code», the check principles
depot2 merge                       # the merge queue: the owner's word, one PR at a time (merge-queue.json); no …
depot2 merge add                   # a PR into the queue, on the owner's word (verbatim; refused without it)
depot2 merge move                  # a waiting or stopped entry to another place (a fix rides first)
depot2 merge handoff               # a merged PR whose ride did not end stops being watched: its deploy rides with …
depot2 merge run                   # the one runner: the PRs one at a time, in order (exit 0 empty, 1 stopped, 3 left …
depot2 merge resume                # the stopped PR waits again (after the fix): from the start, or from the deploy …
depot2 merge drop                  # out of the queue (a stopped one: the queue goes on)
depot2 merge rerun                 # gh run rerun --failed for the run GitHub left unstarted («waiting for GitHub»)
depot2 merge prune-worktrees       # the worktrees of merged branches under merge.worktrees removed by the rules of …
depot2 merge report                # short, for the owner: the last merges, their deploy, smoke, the Actions minutes …
depot2 merge resolve               # prepared conflict resolutions, keyed by the two sides' blobs
depot2 merge resolve add           # the resolved file for one conflict (ours = the branch's blob, theirs = main's)
depot2 merge resolve list          # the prepared resolutions, with their keys
depot2 merge resolve drop          # take one out (the history keeps it)
depot2 queues                      # the heads of every queue with counts (stack, merge queue, team stack, trains, …
depot2 queues migrate              # one shot, idempotent: the stack's autostart = no written explicitly, every car's …
depot2 gender                      # set a character's grammatical gender (meta.json + spec.md)
depot2 decide                      # record the owner's decision about a character (or all)
depot2 decisions                   # the owner's decisions (decisions.jsonl), newest first; only reads (decide writes)
depot2 reflect                     # rewrite the portraits the owner's actions touched
depot2 backup                      # the state's backup: a git push every 10 commits, Drive by its flag (no option: …
depot2 config                      # print the effective config (defaults + config.json + --set)
depot2 ledger                      # balances, open stakes, pending awards
depot2 show                        # a character's portrait, thoughts and stakes
depot2 init                        # a new, empty depot at --state: its git, config.json, the team's specs and balances
depot2 seed                        # one-off: state, specs, ledger, first portraits
```

## Tests

```sh
python3 -m unittest discover -s tests            # the standard library only, no network
python3 -m unittest discover -s tests -p 'test_merge*.py'
```

No test reaches GitHub, a runner or the network: `tests/` has stand-ins for `gh`, `git` (a logging wrapper), `rclone`,
`pass`, the scan and the executor (`fake_executor.py`), and the test project's config (`tests/project-config.json`,
through `$DEPOT2_CONFIG`). `tests/test_demo.py` builds the demo depot and reads it back. `tests/test_cli_docs.py`
checks that every command this README names exists, and that the «Commands» block above names every command.

## Layout

```
depot2/              the package: cli.py (the command line), one module per command or store
depot2/executors/    the executor interface; cli_agent.py, the bundled adapter
depot2/hooks.py      the plugin interface
depot2/web/          the web view's one page (HTML, CSS and JS, nothing from outside)
examples/demo/       make-demo.sh: an invented project's depot
examples/plugins/    example_checks.py: a neutral plugin
depot2/catalogs/     the message catalogs: en.py (the default) and ru.py; depot2/i18n.py reads them
tests/               the tests and their stand-ins
```

## License

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE).
