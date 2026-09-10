{
    'name': 'Baseer Sales Summary Dashboard',
    'version': '19.0.1.1.4',
    'category': 'Sales/Point of Sale',
    'license': 'LGPL-3',
    'depends': ['baseer_pos_summary', 'spreadsheet_dashboard'],
    'data': ['data/dashboard.xml'],
    'assets': {
        'spreadsheet.o_spreadsheet': [
            'baseer_sales_dashboard/static/src/**/*.js',
            'baseer_sales_dashboard/static/src/**/*.xml',
        ],
        'web.assets_backend': ['baseer_sales_dashboard/static/src/**/*.scss'],
    },
    'installable': True,
}
