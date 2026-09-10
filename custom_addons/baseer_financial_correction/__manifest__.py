{
    'name': 'Baseer Financial Correction',
    'summary': 'Reviewed source-aware correction of financial input errors',
    'version': '19.0.1.0.0',
    'author': 'Baseer',
    'license': 'LGPL-3',
    'depends': ['account', 'baseer_access_roles', 'baseer_financial_register', 'baseer_purchase_batch'],
    'data': [
        'security/ir.model.access.csv',
        'security/correction_rules.xml',
        'views/correction_views.xml',
    ],
    'installable': True,
    'application': False,
}
