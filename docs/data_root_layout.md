# vnpy Data Root Layout

The default deployment surface is one variable: `VNPY_DATA_ROOT`.
Legacy variables such as `QS_DATA_ROOT`, `ML_OUTPUT_ROOT`, `VNPY_MODEL_ROOT`,
`LOG_ROOT`, `BACKUP_ROOT`, and `REPLAY_HISTORY_DB` are kept only as advanced
explicit overrides or migration compatibility.

Recommended layout:

```text
<VNPY_DATA_ROOT>/
  config/
    signal_dual_track.json
  state/
    replay_history.db
    event_journal.db
    sim_<gateway>.db
  ml_output/
  snapshots/
    merged/
    filtered/
  models/
  logs/
  backups/
```

Minimal config:

```ini
VNPY_DATA_ROOT=C:/path/to/vnpy_data
# Optional; omit it to use <VNPY_DATA_ROOT>/config/signal_dual_track.json.
# SIGNAL_DUAL_TRACK_CONFIG=C:/path/to/signal_dual_track.json
```

Migration is dry-run by default and never deletes legacy files:

```powershell
cd F:\Quant\vnpy\vnpy_strategy_dev
.\deploy\migrate_data_root.ps1 -DataRoot C:\path\to\vnpy_data
.\deploy\migrate_data_root.ps1 -DataRoot C:\path\to\vnpy_data -Execute
```

The migration report is written under `<VNPY_DATA_ROOT>/backups/`.
After services run correctly from the new root, clean old files manually.

Backup:

```powershell
.\deploy\daily_backup.ps1 -VnpyDataRoot C:\path\to\vnpy_data
```

`daily_backup.ps1` backs up `replay_history.db`, `event_journal.db`, `sim_*.db`,
vntrader config/database files, and model metadata. The mlearnweb database is
backed up separately from `MLEARNWEB_DATA_ROOT`.
