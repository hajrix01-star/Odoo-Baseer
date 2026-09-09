{
    'name': 'Baseer Browser Print',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Print original PDF reports and keep a separate PDF download',
    'author': 'Baseer',
    'license': 'LGPL-3',
    'depends': ['web'],
    'assets': {'web.assets_backend': [
        'baseer_browser_print/static/src/report_transport.js',
        'baseer_browser_print/static/src/print_dialog.js',
        'baseer_browser_print/static/src/print_dialog.xml',
        'baseer_browser_print/static/src/print_dialog.scss',
    ]},
    'installable': True,
}
