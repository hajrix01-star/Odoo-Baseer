def migrate(cr, version):
    cr.execute('ALTER TABLE baseer_pos_summary DROP CONSTRAINT IF EXISTS baseer_pos_summary_company_date_period_unique')
