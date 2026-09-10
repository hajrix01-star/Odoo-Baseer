{
    'name': 'Baseer Payroll and Employee Advances',
    'version': '19.0.1.5.1',
    'license': 'LGPL-3',
    'depends': ['om_hr_payroll_account', 'hr_holidays', 'baseer_report_layout'],
    'data': [
        'data/work_schedules.xml',
        'security/security.xml', 'security/ir.model.access.csv',
        'views/payroll_views.xml', 'views/loan_views.xml',
        'views/settings_views.xml', 'report/payslip_report.xml',
        'views/settlement_views.xml', 'report/payment_receipt.xml',
        'security/end_service_security.xml', 'views/end_service_views.xml',
        'views/eos_workflow_views.xml', 'report/end_service_report.xml',
        'views/correction_views.xml',
    ],
    'assets': {'web.assets_backend': ['baseer_payroll/static/src/payroll.scss', 'baseer_payroll/static/src/payroll_month_field.js', 'baseer_payroll/static/src/payroll_month_field.xml']},
    'installable': True,
}
