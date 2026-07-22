# a 2026-07-18 # P1 minute layer verification
memory:
  p1_minute_layer: complete
  p1_table: stock_minute (19 fields, 5 indexes)
  p1_script: minute_collector.py (410 lines, with preflight check)
  p1_cron: run_daily.sh step 6.6 (weekdays 17:00)
  p1_changelog: changelog_p1_minute_layer.md (3923 bytes)
  p1_stk_mins_status: penalty_blocked (test exceeded hourly limit)
  p1_prerequisites: [P0_sina_fix, stk_mins_cooldown]
  p1_start_condition: collector auto-checks stk_mins before run, skips if blocked
