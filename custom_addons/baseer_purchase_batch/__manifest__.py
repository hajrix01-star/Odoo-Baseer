{
    'name': 'Baseer Purchase Batch',
    'version': '19.0.1.2.3',
    'category': 'Accounting/Accounting',
    'summary': 'Approve supplier bill batches using native Odoo accounting',
    'license': 'LGPL-3',
    'depends': ['account', 'baseer_report_layout', 'baseer_category_display'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'views/purchase_batch_views.xml',
        'views/category_mapping_views.xml',
        'views/res_partner_views.xml',
        'report/purchase_batch_report.xml',
    ],
    'application': False,
    'installable': True,
    'assets': {
        'web.assets_backend': [
            'baseer_purchase_batch/static/src/js/latin_date_field.js',
            'baseer_purchase_batch/static/src/scss/purchase_batch.scss',
        ],
    },
}
