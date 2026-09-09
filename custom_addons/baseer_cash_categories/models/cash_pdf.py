from decimal import Decimal, ROUND_HALF_UP

from odoo import _, api, models
from odoo.addons.eh_account_base.tools.xlsx_writer import XlsxReportWriter
from openpyxl.styles import Alignment


class BaseerCashXlsxWriter(XlsxReportWriter):
    def _write_meta(self, meta, generated_at):
        months = meta.get('baseer_months')
        if not months:
            return super()._write_meta(meta, generated_at)
        super()._write_meta(dict(meta, date_from=None, date_to=None), generated_at)
        bands = [', '.join(months[i:i+3]) for i in range(0, len(months), 3)]
        cell = self.ws.cell(row=self._row, column=1,
                            value=self.baseer_month_label + '\n' + '\n'.join(bands))
        cell.font = self.META_FONT
        cell.alignment = Alignment(vertical='top', wrap_text=True)
        self.ws.row_dimensions[self._row].height = 15 * (len(bands) + 1)
        self._row += 1

    def _apply_figure_type(self, cell, figure_type):
        super()._apply_figure_type(cell, figure_type)
        if figure_type == 'baseer_percentage':
            # Values already are rounded percentage points from the handler.
            cell.number_format = '0.00"%"'


class BaseerCashExports(models.Model):
    _inherit = 'eh.account.dynamic.report'

    def render_pdf(self, options, use_cache=True):
        self.ensure_one()
        if self.code == 'baseer_cash_categories':
            return super(BaseerCashExports, self.with_context(
                baseer_cash_pdf=True, baseer_cash_pdf_report_id=self.id)).render_pdf(
                options, use_cache=use_cache)
        return super(BaseerCashExports, self.with_context(
            baseer_cash_pdf=False, baseer_cash_pdf_report_id=False)).render_pdf(
                options, use_cache=use_cache)

    def render_xlsx(self, options, use_cache=True):
        self.ensure_one()
        if self.code != 'baseer_cash_categories':
            return super().render_xlsx(options, use_cache=use_cache)
        self._eh_check_access('read')
        options = dict(options or {}, eager_expand=True)
        options.pop('lazy_expand', None)
        writer = BaseerCashXlsxWriter(report_name=self.name)
        writer.baseer_month_label = _('Selected months:')
        return self._eh_render_result(options, result_format='xlsx', use_cache=use_cache,
                                      result_builder=writer.write_payload)


class BaseerCashPaperFormat(models.Model):
    _inherit = 'ir.actions.report'

    def get_paperformat(self):
        report_id = self.env.context.get('baseer_cash_pdf_report_id')
        cash_report = (self.env['eh.account.dynamic.report'].browse(report_id).exists()
                       if type(report_id) is int else self.env['eh.account.dynamic.report'])
        if (self.env.context.get('baseer_cash_pdf')
                and cash_report and cash_report.code == 'baseer_cash_categories'
                and self.report_name == 'eh_account_dynamic_reports.report_dynamic_pdf_template'):
            return self.env.ref('baseer_cash_categories.paperformat_cash_portrait')
        return super().get_paperformat()


class BaseerCashPdfStyle(models.AbstractModel):
    _inherit = 'report.eh_account_dynamic_reports.report_dynamic_pdf_template'

    @api.model
    def _get_report_values(self, docids, data=None):
        values = super()._get_report_values(docids, data=data)
        for report in values['rendered']:
            if report['doc'].code == 'baseer_cash_categories':
                report['baseer_labels'] = {
                    'months': _('Selected months:'), 'currency': _('Currency:'),
                    'sales': _('Evidenced sales collections:'),
                    'tax': _('Include VAT') if report['payload']['meta'].get('baseer_include_tax', True) else _('Exclude VAT'),
                }
        return values

    @api.model
    def _line_css_class(self, line):
        classes = super()._line_css_class(line)
        role = (line.get('meta') or {}).get('baseer_role')
        return classes + (' baseer_pdf_' + role if role else '')

    @api.model
    def _render_lines(self, payload):
        formatted_payload = payload
        if self.env.context.get('baseer_cash_pdf'):
            formatted_payload = dict(payload, currency=dict(payload.get('currency') or {}, symbol=''))
        lines = super()._render_lines(formatted_payload)
        for source, rendered in zip(payload.get('lines') or [], lines):
            for index, (cell, formatted) in enumerate(zip(source.get('columns') or [], rendered['cells'])):
                definition = (payload.get('columns') or [])[index + 1]
                if (cell.get('figure_type') or definition.get('figure_type')) == 'baseer_percentage':
                    formatted.update(display=cell.get('display_value') or '', align_right=True)
        if (self.env.context.get('baseer_cash_pdf')
                and (payload.get('meta') or {}).get('report_code') == 'baseer_cash_categories'):
            # Screen and XLSX keep the full reconciliation appendix. Print only
            # the operating report, unless a monthly reconciliation needs attention.
            decimals = (payload.get('currency') or {}).get('decimal_places', 2)
            quantum = Decimal(1).scaleb(-int(2 if decimals is None else decimals))
            visible = []
            for source, rendered in zip(payload.get('lines') or [], lines):
                appendix = (source.get('id') == 'baseer-reconciliation'
                            or source.get('parent_id') == 'baseer-reconciliation')
                if appendix:
                    discrepancy = source.get('id') == 'baseer-total-balance_check' and any(
                        Decimal(str(cell.get('value') or 0)).quantize(quantum, rounding=ROUND_HALF_UP)
                        for cell, definition in zip(source.get('columns') or [], payload['columns'][1:])
                        if (cell.get('figure_type') or definition.get('figure_type')) == 'monetary')
                    if not discrepancy:
                        continue
                    rendered = dict(rendered, level=0)
                visible.append(rendered)
            lines = visible
        return lines

    @api.model
    def _build_pdf_table_chunks(self, columns, lines, header_rows=None):
        if not self.env.context.get('baseer_cash_pdf') or len(columns) <= 6:
            return super()._build_pdf_table_chunks(columns, lines, header_rows)
        chunks = []
        value_count = len(columns) - 1
        for start in range(0, value_count, 5):
            stop = min(start + 5, value_count)
            indexes = [0] + list(range(start + 1, stop + 1))
            panel_columns = [columns[i] for i in indexes]
            panel_lines = [dict(line, cells=line.get('cells', [])[start:stop]) for line in lines]
            panel_headers = self._slice_pdf_header_rows(columns, header_rows or [], indexes)
            panel = super()._build_pdf_table_chunks(panel_columns, panel_lines, panel_headers)[0]
            panel.update(index=len(chunks), value_from=start + 1, value_to=stop,
                         value_count=value_count)
            chunks.append(panel)
        for panel in chunks:
            panel['count'] = len(chunks)
        return chunks
