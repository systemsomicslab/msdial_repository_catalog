# Scheduled catalog updates

The GUI, CLI, and MCP use the same update-job implementation. Metadata is
downloaded sequentially from public repository services; raw mass-spectrometry
data are never downloaded by this operation.

## Update scopes

- `indexed`: refresh only accessions already stored locally. Use this for routine
  bounded updates.
- `unindexed`: retrieve the current accession index, subtract accessions already
  stored locally, then apply `--limit`. Use repeated bounded runs to grow a
  catalog without re-reading earlier records.
- `discover`: retrieve the current accession index, then fetch every selected
  record, including indexed accessions. Use this for a full refresh. A full run
  can take hours and depends on external services.

Run all sources from a terminal:

```powershell
msdial-repository-catalog --database D:\MSDIAL_Catalog\catalog.sqlite update --mode indexed
```

Grow the catalog in repeatable batches:

```powershell
msdial-repository-catalog --database D:\MSDIAL_Catalog\catalog.sqlite update `
  --mode unindexed --repository metabolomics_workbench --limit 100
```

Run a full refresh for selected sources or a bounded test:

```powershell
msdial-repository-catalog --database D:\MSDIAL_Catalog\catalog.sqlite update `
  --mode discover --repository metabolights --repository mb_post --limit 100
```

The command exits with a nonzero code when one or more accessions fail, making
it suitable for scheduler failure notifications.

## Windows Task Scheduler

Create a task that runs while the machine and network are available. Set
`Program/script` to the virtual environment's `python.exe`, `Start in` to this
repository, and use arguments such as:

```text
-m msdial_repository_catalog.cli --database D:\MSDIAL_Catalog\catalog.sqlite update --mode indexed
```

A practical cadence is a frequent indexed refresh, regular unindexed-only
growth, and a less frequent full discovery run. Do not schedule overlapping tasks; the GUI/MCP process prevents overlap
inside one process, while separate operating-system processes cannot coordinate.

## macOS and Linux cron

Example weekly discovery at 03:00 on Sunday:

```cron
0 3 * * 0 /opt/msdial-catalog/.venv/bin/python -m msdial_repository_catalog.cli --database /srv/msdial/catalog.sqlite update --mode discover >> /var/log/msdial-catalog.log 2>&1
```

Use an operating-system lock (`flock` on Linux) when another process may update
the same SQLite file.

## Claude, Cowork, and Codex

An MCP client can call `msdial_catalog_update_start` with `confirmed=true`, then
poll `msdial_catalog_update_status` until the state is terminal. Cancellation is
cooperative: `msdial_catalog_update_cancel` stops after the current accession.

For unattended execution, an OS scheduler is the most robust owner because the
task survives desktop-app restarts. Agent scheduling is useful when the agent
must inspect the result, summarize failures, or decide whether to run a bounded
retry. In either case, the computer must be awake and able to reach the public
repository services.
