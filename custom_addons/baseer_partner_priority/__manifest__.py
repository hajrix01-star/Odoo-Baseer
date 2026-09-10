{
    'name': 'Baseer Partner Favorites',
    'version': '19.0.1.0.0',
    'summary': 'Company favorites followed by recent supplier usage',
    'category': 'Accounting/Accounting',
    'license': 'LGPL-3',
    'depends': ['account', 'baseer_purchase_batch'],
    'data': ['views/partner_views.xml', 'views/purchase_batch_views.xml'],
    'assets': {
        'web.assets_backend': [
            'baseer_partner_priority/static/src/partner_autocomplete.js',
            'baseer_partner_priority/static/src/partner_autocomplete.xml',
        ],
    },
    'installable': True,
}
