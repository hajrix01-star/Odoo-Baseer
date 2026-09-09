{
    'name': 'Baseer Simple Work Schedules',
    'version': '19.0.1.1.2',
    'author': 'Baseer',
    'license': 'LGPL-3',
    'depends': ['baseer_payroll', 'hr_attendance'],
    'data': ['security/ir.model.access.csv', 'security/rules.xml', 'data/weekdays.xml', 'views/schedule_views.xml'],
    'assets': {'web.assets_backend': [
        'baseer_work_schedule/static/src/schedule.scss',
        'baseer_work_schedule/static/src/time_field.js',
        'baseer_work_schedule/static/src/time_field.xml',
    ]},
    'installable': True,
}
