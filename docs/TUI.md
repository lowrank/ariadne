# Ariadne terminal user interface

Launch:

```bash
ariadne tui PROJECT --config PROJECT/ariadne.codex.toml
# or use the current directory and let the TUI choose the config
ariadne-tui
ariadne tui
```

When `PROJECT/.ariadne` does not exist, the TUI opens the setup interview
immediately. It bootstraps the Codex TOML when needed, then generates the
problem contract JSON and updates the TOML from the answers. For an existing
`.ariadne` project, it asks whether the latest paused campaign should resume.
If the TOML is missing, the setup interview asks for a task description or one
task file, followed by the operator-controlled mode, agent, and budget settings. The bounded agents infer the title and
mathematical structure, then complete the TOML and contract JSON.

## Panels

- Campaign/current plan: target, mode, status, epoch, active public task summaries.
- Active and queued tasks: persistent `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, or `CANCELLED` records.
- Routes: status, title, and key bridge lemma.
- Partial results: claims and recent attempt summaries.
- Failure clusters: canonical obstruction classes and repeat counts.
- Budget and controls: call budget, cost, pause state, instructions, TUI status.
- Artifacts: recent artifacts and a text preview selected with `/artifact next` or `/artifact prev`.
- Recent activity: up to 1,000 durable activity events with a scrollbar for older entries.

## Slash commands

Enter commands in the bottom-line prompt. Typing `/` opens a popup of matching commands with a short explanation for each command. Actions are durable through the same
CLI/store control plane as the non-TUI interface.

```text
/run                         start a campaign or resume a paused campaign
/pause                       request a safe pause
/recover                     recover stale interrupted work into a safe pause
/budget                      interactively adjust limits for a paused or exhausted campaign
/budget E C USD [reason]     set maximum epochs, calls, and cost directly
/instruct                    add a persistent human instruction
/route                       change a route status
/setup                       run setup interview and generation
/artifact next|prev          select an artifact preview
/model                       select the Codex model and reasoning strength
/report [path]               generate the report and continuation handoff brief
/refresh                     refresh the display
/help                        show command help
/quit                        quit the TUI
```


The TUI starts campaign processes as subprocesses so it remains responsive and can write pause/instruction records while agents are active.

Quitting the TUI does not kill an active campaign subprocess. This is deliberate:
the persistent controller can continue, and the operator can reopen the TUI or
use the CLI from another terminal. Request a safe pause before quitting when the
campaign should stop at the next controller boundary.
