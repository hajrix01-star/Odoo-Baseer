{
    'name': 'Baseer Financial Operations',
    'summary': 'Posted financial operations with native settlement and invoice indicators',
    'version': '19.0.1.2.1',
    'author': 'Baseer',
    'license': 'LGPL-3',
    'depends': ['account', 'baseer_access_roles', 'baseer_report_layout', 'baseer_cash_categories'],
    'data': ['views/financial_register_views.xml'],
    'assets': {'web.assets_backend': [
        'baseer_financial_register/static/src/*.js',
        'baseer_financial_register/static/src/*.xml',
        'baseer_financial_register/static/src/*.scss',
    ]},
    'installable': True,
    'application': False,
}
