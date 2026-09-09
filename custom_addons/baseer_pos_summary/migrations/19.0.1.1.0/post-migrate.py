def migrate(cr, version):
    # New operational metadata only: retain every original sale and posting.
    cr.execute("""UPDATE baseer_pos_summary
                     SET day_schedule = CASE WHEN period_scope = 'all' THEN 'all' ELSE 'split' END
                   WHERE day_schedule IS NULL OR (day_schedule = 'all' AND period_scope != 'all')""")
