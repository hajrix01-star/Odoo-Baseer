{
    'name': 'Baseer Employee Services',
    'summary': 'Employee services linked to native supplier bills and payments',
    'version': '19.0.1.1.0',
    'license': 'LGPL-3',
    'depends': ['baseer_service_seed', 'baseer_payroll'],
    'data': [
        'security/ir.model.access.csv', 'security/security.xml',
        'data/sequence.xml', 'views/service_views.xml',
        'views/employee_views.xml', 'report/service_report.xml',
    ],
    'installable': True,
}
