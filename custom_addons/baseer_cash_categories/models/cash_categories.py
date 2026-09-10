"""Evidence-based cash allocation. Reporting only; never modifies ledger data."""
from collections import defaultdict
from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, localcontext
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


CENT = Decimal('0.01')
ZERO = Decimal('0.00')


def money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def round_shares(exact_shares, total):
    """Largest fractional-cent remainder, with input order as stable tie break.

    Flooring signed shares then adding cents preserves legitimate negative
    discount weights without inventing the opposite sign in an ordinary split.
    ``total`` is the independently rounded target, including when it is zero.
    """
    if not exact_shares:
        return []
    cents = [share / CENT for share in exact_shares]
    floors = [value.to_integral_value(rounding=ROUND_FLOOR) for value in cents]
    remainder = int(total / CENT - sum(floors, ZERO))
    if remainder < 0 or remainder > len(floors):
        raise ValueError('Rounded target is inconsistent with exact allocations')
    order = sorted(range(len(cents)), key=lambda index: (-(cents[index] - floors[index]), index))
    for index in order[:remainder]:
        floors[index] += 1
    return [value * CENT for value in floors]


def allocate(total, weights):
    """Proportionally split cents using deterministic largest remainders."""
    denominator = sum(weights, ZERO)
    if not weights or not denominator:
        return []
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        return round_shares([total * weight / denominator for weight in weights], total)


class BaseerCashCategoryHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.baseer_cash_categories'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = 'Baseer cash movement by category'

    REPORT_CODE = 'baseer_cash_categories'
    REPORT_NAME = 'Cash movement by category'
    _BASEER_LIMIT = 10000
    _BASEER_AGGREGATE_LIMIT = 30000
    _BASEER_ROW_LIMIT = 1000

    @api.model
    def build_default_options(self):
        options = super().build_default_options()
        options.update(baseer_include_tax=True, posted_only=True,
                       baseer_months=[fields.Date.context_today(self).strftime('%Y-%m')])
        return options

    @api.model
    def normalize_options(self, options):
        options = options or {}
        self._validate_filters(options)
        result = super().normalize_options(options)
        result['baseer_include_tax'] = options.get('baseer_include_tax', True)
        if 'baseer_months' in options:
            result['baseer_months'] = self._validate_months(options['baseer_months'])
        return result

    def _validate_months(self, months):
        if not isinstance(months, list) or not 1 <= len(months) <= 12:
            raise UserError(_('Select between 1 and 12 calendar months.'))
        if any(not isinstance(month, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', month)
               for month in months):
            raise UserError(_('Months must use YYYY-MM format.'))
        if len(set(months)) != len(months):
            raise UserError(_('Select each month only once.'))
        periods = sorted(months)
        try:
            for period in periods:
                date(int(period[:4]), int(period[5:]), 1)
        except ValueError:
            raise UserError(_('Invalid calendar month.')) from None
        span = (int(periods[-1][:4]) - int(periods[0][:4])) * 12 + int(periods[-1][5:]) - int(periods[0][5:])
        if span >= 24:
            raise UserError(_('Selected months must fit within a 24-month span.'))
        return periods

    def _validate_filters(self, options):
        unsupported = ('journal_ids', 'partner_ids', 'account_ids', 'account_type_ids',
                       'analytic_account_ids', 'analytic_plan_ids',
                       'analytic_column_account_ids', 'analytic_column_plan_ids',
                       'account_tag_ids', 'tax_ids', 'cash_basis', 'comparative',
                       'cash_equivalent_account_ids', 'horizontal_group_by',
                       'cf_interest_paid_section', 'cf_dividends_paid_section', 'show_zero')
        active = [key for key in unsupported if options.get(key)]
        if options.get('comparison', 'none') not in (None, False, 'none'):
            active.append('comparison')
        if options.get('posted_only', True) is not True:
            active.append('posted_only=False')
        if options.get('cash_flow_method', 'direct') != 'direct':
            active.append('cash_flow_method')
        if options.get('reconcile_state', 'open') != 'open':
            active.append('reconcile_state')
        if (options.get('date') or {}).get('mode', 'range') == 'as_of':
            active.append('date.mode')
        if options.get('hierarchical_groups', True) is False:
            active.append('hierarchical_groups=False')
        if active:
            raise UserError(_('This cash report does not support these filters: %s') % ', '.join(active))
        if not isinstance(options.get('baseer_include_tax', True), bool):
            raise UserError(_('Include tax must be a boolean.'))

    def _consume_budget(self, budget, count):
        if budget is not None:
            budget['used'] += count
            if budget['used'] > self._BASEER_AGGREGATE_LIMIT:
                raise UserError(_('The monthly preview limit is 30,000 read and trace records. Select fewer months.'))

    def _bounded(self, model, domain, order='id', budget=None):
        records = self.env[model].search(domain, order=order, limit=self._BASEER_LIMIT + 1)
        if len(records) > self._BASEER_LIMIT:
            raise UserError(_('The preview limit is 10,000 records. Use a smaller date range.'))
        self._consume_budget(budget, len(records))
        return records

    def _path(self, line):
        category = line.product_id.categ_id
        if category:
            path, seen = [], set()
            while category:
                if category.id in seen or len(seen) >= 30:
                    raise UserError(_('Invalid or excessively deep product category tree.'))
                seen.add(category.id)
                path.insert(0, ('category-%s' % category.id, category.name))
                category = category.parent_id
            return path
        group, path, seen = line.account_id.group_id, [], set()
        while group:
            if group.id in seen or len(seen) >= 30:
                raise UserError(_('Invalid or excessively deep account group tree.'))
            seen.add(group.id)
            path.insert(0, ('group-%s' % group.id, group.name))
            group = group.parent_id
        path.append(('ledger-%s' % line.account_id.id, line.account_id.name))
        return path

    def _unknown(self, amount, line, state, reason):
        state['diagnostics'].add(reason)
        return [{'gross': amount, 'net': amount,
                 'path': [('unclassified', _('Unclassified / needs allocation'))],
                 'source': line.id, 'unknown': True}]

    def _invoice(self, invoice, amount, source, state):
        # Posted line prices are the invoice's tax evidence; no fixed VAT rate.
        lines = invoice.invoice_line_ids.filtered(lambda row: row.display_type == 'product').sorted('id')
        state['visits'] += len(lines)
        self._check_budget(state)
        if invoice.currency_id != invoice.company_id.currency_id:
            return self._unknown(amount, source, state, _('Foreign-currency invoice: tax allocation needs review.'))
        gross = [money(line.price_total) for line in lines]
        net = [money(line.price_subtotal) for line in lines]
        total = sum(gross, ZERO)
        # Manual tax edits / cash-rounding differences cannot be guessed.
        if (not total or any(not value and untaxed for value, untaxed in zip(gross, net))
                or total != money(invoice.amount_total)
                or sum(net, ZERO) != money(invoice.amount_untaxed)):
            return self._unknown(amount, source, state, _('Invoice rounding or adjusted taxes: allocation needs review.'))
        shares = allocate(amount, gross)
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            # Use gross invoice total as denominator even when signed untaxed
            # lines cancel to zero. Their individual category effects remain.
            exact_net_shares = [amount * value / total for value in net]
            net_total = sum(exact_net_shares, ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
            net_shares = round_shares(exact_net_shares, net_total)
        result = []
        for line, value, excluded in zip(lines, shares, net_shares):
            path = self._path(line)
            if invoice.move_type == 'out_refund':
                path.insert(0, ('customer-refunds', _('Customer refunds')))
            result.append({'gross': value, 'net': excluded, 'path': path,
                           'source': line.id, 'invoice_id': invoice.id, 'unknown': False,
                           'sale_collection': invoice.move_type in ('out_invoice', 'out_receipt') and amount > ZERO})
        return result

    def _check_budget(self, state):
        previous = state.get('budget_visits', 0)
        self._consume_budget(state.get('budget'), state['visits'] - previous)
        state['budget_visits'] = state['visits']
        if state['visits'] > self._BASEER_LIMIT:
            raise UserError(_('The reconciliation preview limit is 10,000 visited lines. Use a smaller date range.'))

    def _baseer_cash_source_kind(self, move):
        """Optional source links are evidence, never a dependency or an ACL grant."""
        for field, kind in (('baseer_loan_id', 'advance'), ('baseer_payslip_id', 'payroll'),
                            ('baseer_correction_payslip_id', 'payroll')):
            if field in move._fields and move[field]:
                return kind, move[field]
        return None, None

    def _baseer_cash_source_boundary(self, line, amount, state):
        """Stop at a proven HR journal instead of traversing its recovery graph.

        ``None`` leaves ordinary accounting to the generic tracer. Payroll
        allocates the actual settled portion across its signed non-payable
        source lines, including deductions, using the net liability once.
        An advance cash leg keeps its receivable category even after recovery.
        """
        move = line.move_id
        kind, source = self._baseer_cash_source_kind(move)
        if not kind:
            return None
        line.check_access('read')
        move.check_access('read')
        if move.company_id.id != state['company_id'] or line.company_id != move.company_id:
            raise AccessError(_('Cash source evidence is outside the report company.'))
        if move.state != 'posted' or move.date > state['date_to'] or line.date > state['date_to']:
            return self._unknown(amount, line, state, _('Counterpart outside the posted reporting cutoff.'))
        if not source.has_access('read'):
            return self._unknown(amount, line, state, _('Employee cash source details are not accessible; the cash amount is retained.'))
        source.check_access('read')
        if source.company_id != move.company_id:
            raise AccessError(_('Employee cash source is outside the report company.'))
        cache = state.setdefault('baseer_cash_source_evidence', {})
        if move.id not in cache:
            rows = move.line_ids.filtered(lambda row: row.balance).sorted('id')
            rows.check_access('read')
            state['visits'] += len(rows)
            self._check_budget(state)
            valid = (move.currency_id == move.company_id.currency_id
                     and all(row.company_id == move.company_id
                             and row.currency_id == move.company_id.currency_id for row in rows)
                     and sum((money(row.balance) for row in rows), ZERO) == ZERO)
            if kind == 'advance':
                treasury = rows.filtered(lambda row: row.account_id.account_type == 'asset_cash')
                principal = rows - treasury
                valid = (valid and len(treasury) == 1 and len(principal) == 1
                         and principal.account_id.account_type == 'asset_receivable'
                         and not rows.tax_line_id)
                cache[move.id] = {'kind': kind, 'valid': valid, 'principal': principal}
            else:
                payable = rows.filtered(lambda row: row.account_id.account_type == 'liability_payable')
                evidence = rows - payable
                valid = (valid and bool(payable) and bool(evidence)
                         and len(payable.account_id) == 1 and len(payable.partner_id) == 1
                         and not rows.filtered(lambda row: row.account_id.account_type == 'asset_cash')
                         and not rows.tax_line_id
                         and bool(sum((money(row.balance) for row in evidence), ZERO)))
                cache[move.id] = {'kind': kind, 'valid': valid, 'payable': payable,
                                  'lines': evidence,
                                  'weights': [money(row.balance) for row in evidence]}
        evidence = cache[move.id]
        if not evidence['valid']:
            return self._unknown(amount, line, state, _('Employee cash source accounting is incomplete; allocation needs review.'))
        if kind == 'advance':
            if line != evidence['principal']:
                return self._unknown(amount, line, state, _('Employee advance cash counterpart is not its evidenced principal.'))
            return [{'gross': amount, 'net': amount, 'path': self._path(line),
                     'source': line.id, 'unknown': False}]
        if line not in evidence['payable']:
            return self._unknown(amount, line, state, _('Payroll cash counterpart is not its evidenced salary liability.'))
        if 'groups' not in evidence:
            groups = {}
            for row, weight in zip(evidence['lines'], evidence['weights']):
                path = self._path(row)
                group = groups.setdefault(tuple(key for key, _name in path),
                    {'path': path, 'rows': [], 'weights': [], 'weight': ZERO})
                group['rows'].append(row)
                group['weights'].append(weight)
                group['weight'] += weight
            evidence['groups'] = list(groups.values())
        groups = evidence['groups']
        fragments = []
        for group, share in zip(groups, allocate(amount, [group['weight'] for group in groups])):
            if not share:
                continue
            # Round each displayed category once, then retain every original
            # expense line for source navigation without changing that total.
            for row, part in zip(group['rows'], allocate(share, group['weights'])):
                if part:
                    fragments.append({'gross': part, 'net': part, 'path': group['path'],
                                      'source': row.id, 'unknown': False})
        return fragments

    def _trace(self, line, amount, state, visited=frozenset(), depth=0):
        """Follow a cash counterpart through partial reconciliations to evidence."""
        if not amount:
            return []
        state['visits'] += 1
        self._check_budget(state)
        line.check_access('read')
        if line.company_id.id != state['company_id']:
            raise AccessError(_('Cross-company reconciliation is not permitted.'))
        if line.id in visited or depth > 6:
            return self._unknown(amount, line, state, _('Unresolved or deep reconciliation chain.'))
        visited = visited | {line.id}
        if line.move_id.state != 'posted' or line.date > state['date_to']:
            return self._unknown(amount, line, state, _('Counterpart outside the posted reporting cutoff.'))
        if line.account_id.account_type == 'asset_cash':
            if line.id in state['cash_ids']:
                return [{'gross': amount, 'net': amount, 'transfer': line.id,
                         'source': line.id, 'unknown': False, 'path': []}]
            return self._unknown(amount, line, state, _('Treasury transfer in transit outside this period.'))
        if line.move_id.is_invoice(include_receipts=True):
            return self._invoice(line.move_id, amount, line, state)
        boundary = self._baseer_cash_source_boundary(line, amount, state)
        if boundary is not None:
            return boundary
        matches = (line.matched_debit_ids | line.matched_credit_ids).sorted('id')
        self._consume_budget(state.get('budget'), len(matches))
        result, used = [], ZERO
        source_portions = {}
        line_balance = abs(money(line.balance))
        if matches and line_balance:
            for match in matches:
                match.check_access('read')
                peer = match.credit_move_id if match.debit_move_id == line else match.debit_move_id
                portion = (amount * money(match.amount) / line_balance).quantize(CENT, rounding=ROUND_HALF_UP)
                # Rounding over-allocation is capped at the evidenced balance.
                if abs(used + portion) > abs(amount):
                    portion = amount - used
                if not portion:
                    continue
                used += portion
                peer.check_access('read')
                if peer.id in visited or peer.company_id.id != state['company_id']:
                    result.extend(self._unknown(portion, line, state, _('Unresolved reconciliation cycle.')))
                elif peer.move_id.is_invoice(include_receipts=True):
                    result.extend(self._trace(peer, portion, state, visited, depth + 1))
                elif self._baseer_cash_source_kind(peer.move_id)[0]:
                    # Several payable credits may belong to one salary source.
                    # Allocate their combined cash portion once, not every
                    # expense/debt sibling once per reconciliation edge.
                    aggregate = source_portions.setdefault(peer.move_id.id, [peer, ZERO])
                    aggregate[1] += portion
                else:
                    others = peer.move_id.line_ids.filtered(lambda row: row.id != peer.id and row.balance).sorted('id')
                    weights = [-money(row.balance) for row in others]
                    if not weights or sum(weights, ZERO) != money(peer.balance):
                        result.extend(self._unknown(portion, line, state, _('Unbalanced counterpart allocation.')))
                    else:
                        for other, share in zip(others, allocate(portion, weights)):
                            result.extend(self._trace(other, share, state, visited | {peer.id}, depth + 1))
            for peer, portion in source_portions.values():
                result.extend(self._baseer_cash_source_boundary(peer, portion, state))
            if amount != used:
                result.extend(self._unknown(amount - used, line, state, _('Unreconciled cash counterpart.')))
            return result
        if line.account_id.account_type in ('asset_receivable', 'liability_payable') or line.account_id.reconcile:
            return self._unknown(amount, line, state, _('Unreconciled cash counterpart.'))
        state['diagnostics'].add(_('Direct journal entries retain their full amount; invoice VAT was not inferred.'))
        return [{'gross': amount, 'net': amount, 'path': self._path(line),
                 'source': line.id, 'unknown': True}]

    @api.model
    def compute(self, options):
        handler = self._authorized_report_handler(options)
        if 'baseer_months' in options:
            return handler._compute_monthly(options)
        return handler._compute_report(options)

    def _authorized_report_handler(self, options):
        """Select one permitted company without depending on header selection.

        Changing a report's company is narrower than changing the user's active
        company context. Validate against assigned companies before constructing
        the temporary ORM context shared by rendering and source navigation.
        """
        requested = options.get('company_ids') or [self.env.company.id]
        if not isinstance(requested, list) or len(requested) != 1:
            raise UserError(_('Select exactly one company for the cash category preview.'))
        if (not isinstance(requested[0], int) or isinstance(requested[0], bool)
                or requested[0] not in self.env.user.company_ids.ids):
            raise AccessError(_('The requested company is outside your authorized companies.'))
        return self.with_context(allowed_company_ids=requested)

    def _compute_report(self, options, with_sources=False, budget=None, internal=None):
        self._validate_filters(options)
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        if date_from > date_to or (date_to - date_from).days > 366:
            raise UserError(_('Choose a valid range of no more than 366 days.'))
        company_ids = options.get('company_ids') or [self.env.company.id]
        if len(company_ids) != 1:
            raise UserError(_('Select exactly one company for the cash category preview.'))
        company = self.env['res.company'].browse(int(company_ids[0])).exists()
        if not company or company.id not in self.env.companies.ids:
            raise AccessError(_('The requested company is outside your current company scope.'))
        company.check_access('read')
        if company.currency_id.name != 'SAR':
            raise UserError(_('This preview supports SAR company ledgers only.'))
        target_currency = options.get('presentation_currency_id')
        if target_currency and int(target_currency) != company.currency_id.id:
            raise UserError(_('Presentation currency conversion is not supported by this preview.'))
        domain = [('company_id', '=', company.id), ('parent_state', '=', 'posted'),
                  ('account_id.account_type', '=', 'asset_cash')]
        cash = self._bounded('account.move.line', domain + [('date', '>=', date_from), ('date', '<=', date_to)], budget=budget)
        opening_lines = self._bounded('account.move.line', domain + [('date', '<', date_from)], budget=budget)
        opening = sum((money(row.balance) for row in opening_lines), ZERO)
        change = sum((money(row.balance) for row in cash), ZERO)
        state = {'company_id': company.id, 'date_to': date_to, 'cash_ids': set(cash.ids),
                 'opening_cash_ids': set(opening_lines.ids),
                 'visits': 0, 'diagnostics': set(), 'budget': budget}
        movements = []
        for move in cash.move_id.sorted('id'):
            treasury = cash.filtered(lambda line: line.move_id == move)
            net_cash = sum((money(row.balance) for row in treasury), ZERO)
            counterparts = move.line_ids.filtered(lambda row: row.account_id.account_type != 'asset_cash' and row.balance).sorted('id')
            if not counterparts and not net_cash:
                continue  # pure same-entry treasury transfer; equal legs cancel
            if sum((-money(row.balance) for row in counterparts), ZERO) != net_cash:
                raise UserError(_('Cash move %s has an inconsistent visible counterpart balance.') % move.display_name)
            fragments = []
            for counterpart in counterparts:
                fragments.extend(self._trace(counterpart, -money(counterpart.balance), state))
            # Credit/debit netting in one move leaves the actual external cash leg.
            for fragment in fragments:
                # Mixed debit/credit treasury moves may contain real receipts
                # and expenses even when their aggregate cash change is zero.
                # Use a same-direction treasury leg when one exists; signed
                # discounts retain the sole actual payment/receipt direction.
                journals = treasury.filtered(lambda row: money(row.balance) * fragment['gross'] > ZERO).sorted('id')
                if not journals:
                    journals = treasury.filtered(lambda row: money(row.balance) * net_cash > ZERO).sorted('id')
                if not journals:
                    if fragment['gross']:
                        raise UserError(_('No treasury leg supports this cash allocation.'))
                    continue
                direction = 'in' if money(journals[0].balance) > ZERO else 'out'
                shares = allocate(fragment['gross'], [abs(money(row.balance)) for row in journals])
                net_shares = allocate(fragment['net'], [abs(money(row.balance)) for row in journals])
                for journal_line, gross, net in zip(journals, shares, net_shares):
                    movements.append(dict(fragment, gross=gross, net=net, cash_source=journal_line.id,
                                          direction=direction,
                                          journal_id=journal_line.journal_id.id,
                                          journal_name=journal_line.journal_id.name,
                                          cash_account_id=journal_line.account_id.id,
                                          account_name=journal_line.account_id.display_name))
        # Reconciled inter-journal transfers cancel only when both in-period legs
        # are evidenced and the excluded signed sum is exactly zero.
        transfers = [item for item in movements if item.get('transfer')]
        transfer_sum = sum((item['gross'] for item in transfers), ZERO)
        if transfer_sum:
            state['diagnostics'].add(_('An incomplete internal transfer remains visible as unclassified.'))
        else:
            movements = [item for item in movements if not item.get('transfer')]
        for item in movements:
            if item.get('transfer'):
                item['path'] = [('unclassified', _('Unclassified / needs allocation'))]
                item['unknown'] = True
        if sum((item['gross'] for item in movements), ZERO) != change:
            raise UserError(_('Cash allocation does not reconcile to the ledger.'))
        return self._payload(movements, opening, change, state, options, company, date_from, date_to,
                             with_sources=with_sources, internal=internal)

    def _payload(self, movements, opening, change, state, options, company, date_from, date_to,
                 with_sources=False, internal=None):
        include_tax = options.get('baseer_include_tax', True)
        nodes = {}
        sources = {}
        receipt_cash_ids, payment_cash_ids, invoice_tax_source_ids = set(), set(), set()
        inflow = outflow = vat = unknown = ZERO
        sales_collections = ZERO
        exact_rows = {}
        def add(path, value, source, source_kind):
            parent = None
            for level, (key, name) in enumerate(path):
                ident = (parent + '/' if parent else '') + key
                node = nodes.setdefault(ident, {'id': ident, 'name': name, 'parent_id': parent,
                                                'level': level, 'amount': ZERO, 'sources': set(),
                                                'source_kind': source_kind})
                node['amount'] += value
                node['sources'].add(source)
                parent = ident
        for item in movements:
            gross = item['gross']
            value = gross if include_tax else item['net']
            if item.get('sale_collection'):
                sales_collections += value
            vat += gross - item['net']
            if item['unknown']:
                unknown += abs(gross)
            if item.get('invoice_id') and gross != item['net']:
                invoice_tax_source_ids.add(item['source'])
            if item['direction'] == 'in':
                inflow += value
                receipt_cash_ids.add(item['cash_source'])
                path = [('receipts', _('Actual receipts')),
                        ('journal-%s' % item['journal_id'], item['journal_name'])]
            else:
                outflow += value
                payment_cash_ids.add(item['cash_source'])
                path = [('payments', _('Actual payments'))] + item['path']
            is_receipt = item['direction'] == 'in'
            add(path, value, item['cash_source'] if is_receipt else item['source'],
                'cash' if is_receipt else 'documents')
        # Keep useful zero-state section totals without adding duplicate rows.
        for ident, name, source_kind in [('receipts', _('Actual receipts'), 'cash'),
                                          ('payments', _('Actual payments'), 'documents')]:
            nodes.setdefault(ident, {'id': ident, 'name': name, 'parent_id': None,
                                     'level': 0, 'amount': ZERO, 'sources': set(),
                                     'source_kind': source_kind})
        lines = []
        def row(ident, name, value, level=0, parent=None, total=False, children=False, count=0,
                role='detail'):
            exact_rows[ident] = value
            return {'id': ident, 'name': name, 'level': level, 'parent_id': parent,
                    'columns': [{'expression_label': 'amount', 'value': float(value)}],
                    'unfoldable': children, 'unfolded': True,
                    'meta': {'kind': 'section_total' if total else 'baseer_cash_category',
                             'source_count': count, 'allocated': True, 'baseer_source_drilldown': True,
                             'baseer_role': role}}
        children = defaultdict(list)
        for node in nodes.values():
            children[node['parent_id']].append(node)
        def emit(parent=None):
            for node in sorted(children[parent], key=lambda node: (node['id'].startswith('payments'), node['name'], node['id'])):
                descendants = children[node['id']]
                # A sole ledger leaf adds no useful level below Customer refunds.
                # Category children and multiple accounts remain individually visible.
                refund_ledger_only = (
                    not (internal and internal.get('preserve_refund_children'))
                    and
                    node['id'].endswith('/customer-refunds')
                    and len(descendants) == 1
                    and descendants[0]['id'].rsplit('/', 1)[-1].startswith('ledger-')
                    and not children[descendants[0]['id']]
                )
                expandable = bool(descendants) and not refund_ledger_only
                if with_sources:
                    sources[node['id']] = {'kind': node['source_kind'], 'aml_ids': node['sources']}
                lines.append(row(node['id'], node['name'], node['amount'], node['level'], node['parent_id'],
                                 children=expandable, count=len(node['sources']),
                                 role='section' if parent is None else 'group' if expandable else 'detail'))
                if not refund_ledger_only:
                    emit(node['id'])
        emit()
        displayed = inflow + outflow
        totals = {'receipts': inflow, 'payments': outflow, 'displayed_net_movement': displayed,
                  'excluded_tax_bridge': ZERO if include_tax else vat,
                  'actual_net_movement': change, 'opening_cash_balance': opening,
                  'closing_cash_balance': opening + change,
                  'balance_check': displayed + (ZERO if include_tax else vat) - change,
                  'unverified_tax_gross': unknown}
        summary_code = 'actual_net_movement' if include_tax else 'displayed_net_movement'
        summary_name = _('Net cash movement') if include_tax else _('Net movement (selected tax basis)')
        detail_totals = []
        if not include_tax:
            detail_totals.extend([
                ('excluded_tax_bridge', _('Excluded invoice tax: bridge to actual cash')),
                ('actual_net_movement', _('Actual net cash movement')),
            ])
        detail_totals.extend([
            ('opening_cash_balance', _('Opening cash balance')),
            ('closing_cash_balance', _('Closing cash balance')),
            ('balance_check', _('Reconciliation difference (must be zero)')),
        ])
        def append_total(code, name, detail=False):
            lines.append(row('baseer-total-' + code, name, totals[code],
                             level=1 if detail else 0,
                             parent='baseer-reconciliation' if detail else None,
                             total=True, role='detail' if detail else 'net'))
            if with_sources:
                source_ids = {
                    'receipts': receipt_cash_ids,
                    'payments': payment_cash_ids,
                    'displayed_net_movement': receipt_cash_ids | payment_cash_ids,
                    'excluded_tax_bridge': invoice_tax_source_ids,
                    'actual_net_movement': state['cash_ids'],
                    'opening_cash_balance': state['opening_cash_ids'],
                    'closing_cash_balance': state['opening_cash_ids'] | state['cash_ids'],
                    'balance_check': state['cash_ids'],
                }[code]
                sources['baseer-total-' + code] = {
                    'kind': 'documents' if code == 'excluded_tax_bridge' else 'cash',
                    'aml_ids': source_ids,
                    'reconciliation': code == 'balance_check',
                }
        append_total(summary_code, summary_name)
        lines.append({'id': 'baseer-reconciliation', 'name': _('Balance reconciliation details'),
                      'level': 0, 'parent_id': None,
                      'columns': [{'expression_label': 'amount', 'value': None}],
                      'unfoldable': True, 'unfolded': False,
                      'meta': {'kind': 'baseer_reconciliation', 'baseer_role': 'group'}})
        for code, name in detail_totals:
            append_total(code, name, detail=True)
        notes = [_('QA allocation: partial payments distributed proportionally across invoice lines.'),
                 _('Only evidenced invoice tax is excluded; unknown tax retains the full cash amount.')]
        notes.extend(sorted(state['diagnostics']))
        for index, note in enumerate(notes):
            lines.append({'id': 'baseer-note-%s' % index, 'name': note, 'level': 1,
                          'parent_id': 'baseer-reconciliation',
                          'columns': [{'expression_label': 'amount', 'value': None}],
                          'unfoldable': False, 'meta': {'kind': 'baseer_note', 'baseer_role': 'note'}})
        # Vendor monetary cells/exporters require JSON numbers. Float conversion
        # happens only at the serialization boundary after exact Decimal maths.
        payload = {'columns': [{'name': _('Description'), 'expression_label': 'name', 'figure_type': 'string'},
                            {'name': _('Amount incl. tax') if include_tax else _('Amount excl. evidenced tax'),
                             'expression_label': 'amount', 'figure_type': 'monetary'}],
                'lines': lines, 'totals': {key: float(value) for key, value in totals.items()},
                'generated_at': fields.Datetime.now().isoformat(),
                'meta': {'report_code': self.REPORT_CODE, 'company_ids': [company.id],
                         'date_from': date_from.isoformat(), 'date_to': date_to.isoformat(),
                         'posted_only': True, 'baseer_include_tax': include_tax,
                         'exact_totals': {key: format(value, '.2f') for key, value in totals.items()},
                         'allocation_policy': 'proportional_invoice_lines_qa',
                         'diagnostics': sorted(state['diagnostics']),
                         'tax_note': _('Only evidenced invoice tax is excluded; unknown tax retains the full cash amount.'),
                         'policy_note': _('QA allocation: partial payments distributed proportionally across invoice lines.'),
                         'total_figure_types': {key: 'monetary' for key in totals}}}
        if len(lines) > self._BASEER_ROW_LIMIT:
            raise UserError(_('The cash preview supports at most 1,000 report rows. Narrow the selection.'))
        if internal is not None:
            internal.update(exact_rows=exact_rows, exact_totals=totals, sales_collections=sales_collections)
        return (payload, sources) if with_sources else payload

    def _compute_monthly(self, options, with_sources=False):
        """Merge exact leaf results; monetary JSON floats are never summed."""
        self._validate_filters(options)
        months = self._validate_months(options.get('baseer_months'))
        budget = {'used': 0}
        snapshots, templates, note_texts = {}, {}, set()
        monthly_columns = []
        source_columns = {}
        for month in months:
            year, month_number = int(month[:4]), int(month[5:])
            start = date(year, month_number, 1).isoformat()
            end = date(year, month_number, monthrange(year, month_number)[1]).isoformat()
            leaf_options = dict(options, date={'mode': 'range', 'date_from': start, 'date_to': end})
            leaf_options.pop('baseer_months', None)
            leaf_options.pop('baseer_drill_column', None)
            internal = {'preserve_refund_children': True}
            leaf_result = self._compute_report(leaf_options, with_sources=with_sources,
                                               budget=budget, internal=internal)
            if with_sources:
                leaf_payload, leaf_sources = leaf_result
            else:
                leaf_payload, leaf_sources = leaf_result, {}
            expression = 'month_' + month.replace('-', '_')
            snapshots[expression] = {'payload': leaf_payload, 'exact': internal,
                                     'sources': leaf_sources}
            source_columns[expression] = leaf_sources
            monthly_columns.append({
                'name': month, 'expression_label': expression, 'figure_type': 'monetary',
                'baseer_month': month,
                'scope': {'date_from': start, 'date_to': end,
                          'company_ids': leaf_payload['meta']['company_ids']},
            })
            for line in leaf_payload['lines']:
                if line.get('meta', {}).get('baseer_role') == 'note':
                    note_texts.add(line['name'])
                else:
                    templates.setdefault(line['id'], line)
            if len(templates) + len(note_texts) > self._BASEER_ROW_LIMIT:
                raise UserError(_('The cash preview supports at most 1,000 report rows. Narrow the selection.'))
        selected_sales = sum((snapshot['exact']['sales_collections'] for snapshot in snapshots.values()), ZERO)
        note_texts.add(_(
            'Sales percentage uses positive collections traced to customer invoices or sales receipts. '
            'Loans, capital, supplier refunds, unclassified receipts and untraced POS are excluded. '
            'Customer refunds remain outgoing and are not deducted from this denominator.'
        ))
        note_texts.add(_('Opening and closing balances are monthly snapshots; their total is intentionally blank.'))
        if selected_sales <= ZERO:
            note_texts.add(_('Sales percentage is unavailable because evidenced sales collections are zero or negative.'))
        snapshot_ids = {'baseer-total-opening_cash_balance', 'baseer-total-closing_cash_balance'}
        exact_rows, total_sources, percentage_sources = {}, {}, {}
        for ident, template in templates.items():
            numeric = bool(template.get('meta', {}).get('baseer_source_drilldown'))
            values = []
            exact_values = []
            for column in monthly_columns:
                expression = column['expression_label']
                snapshot = snapshots[expression]
                value = snapshot['exact']['exact_rows'].get(ident, ZERO) if numeric else None
                exact_values.append(value)
                values.append({'expression_label': expression,
                               'value': float(value) if value is not None else None})
                if with_sources and numeric and ident not in source_columns[expression]:
                    # A category absent this month is an evidenced zero, and its
                    # action is an empty list, never another month's documents.
                    kind = next((other['sources'][ident]['kind'] for other in snapshots.values()
                                 if ident in other['sources']), 'cash')
                    source_columns[expression][ident] = {'kind': kind, 'aml_ids': set()}
            total = (sum(exact_values, ZERO) if numeric and ident not in snapshot_ids else None)
            values.append({'expression_label': 'amount', 'value': float(total) if total is not None else None})
            percentage = None
            if numeric and (ident == 'payments' or ident.startswith('payments/')) and selected_sales > ZERO:
                with localcontext() as decimal_context:
                    decimal_context.prec = 50
                    percentage = (abs(total) / selected_sales * Decimal('100')).quantize(CENT, rounding=ROUND_HALF_UP)
            values.append({'expression_label': 'receipt_share',
                           'value': float(percentage) if percentage is not None else None,
                           'display_value': format(percentage, '.2f') + '%' if percentage is not None else ''})
            templates[ident] = dict(template, columns=values)
            exact_rows[ident] = {
                column['expression_label']: (format(value, '.2f') if value is not None else None)
                for column, value in zip(monthly_columns, exact_values)
            }
            exact_rows[ident].update(amount=format(total, '.2f') if total is not None else None,
                                     receipt_share=format(percentage, '.2f') if percentage is not None else None)
            if with_sources and numeric and total is not None:
                originals = [snapshot['sources'][ident] for snapshot in snapshots.values()
                             if ident in snapshot['sources']]
                source_kind = originals[0]['kind'] if originals else 'cash'
                total_sources[ident] = {
                    'kind': source_kind,
                    'aml_ids': set().union(*(source['aml_ids'] for source in originals)),
                    'reconciliation': any(source.get('reconciliation') for source in originals),
                }
                if percentage is not None:
                    percentage_sources[ident] = dict(total_sources[ident], sales_percentage=True)
        source_columns['amount'] = total_sources
        source_columns['receipt_share'] = percentage_sources
        hierarchy = defaultdict(list)
        for line in templates.values():
            hierarchy[line.get('parent_id')].append(line)
        lines = []
        def emit(parent=None):
            def sort_key(line):
                role = line.get('meta', {}).get('baseer_role')
                if parent is None:
                    rank = 0 if line['id'] == 'receipts' else 1 if line['id'] == 'payments' else 2 if role == 'net' else 3
                    return rank, line['name'], line['id']
                if parent == 'baseer-reconciliation':
                    order = ['excluded_tax_bridge', 'actual_net_movement', 'opening_cash_balance',
                             'closing_cash_balance', 'balance_check']
                    key = line['id'].replace('baseer-total-', '')
                    return order.index(key) if key in order else 99, line['name'], line['id']
                return 0, line['name'], line['id']
            for line in sorted(hierarchy[parent], key=sort_key):
                descendants = hierarchy[line['id']]
                simple_refund = (line['id'].endswith('/customer-refunds')
                                 and len(descendants) == 1
                                 and descendants[0]['id'].rsplit('/', 1)[-1].startswith('ledger-')
                                 and not hierarchy[descendants[0]['id']])
                # Refund simplification may vary with monthly data. If a child
                # exists in any selected month, retain its parent expansion.
                if descendants:
                    line['unfoldable'] = not simple_refund
                lines.append(line)
                if not simple_refund:
                    emit(line['id'])
        emit()
        columns = [{'name': _('Description'), 'expression_label': 'name', 'figure_type': 'string'}]
        columns.extend(monthly_columns)
        column_company_ids = next(iter(snapshots.values()))['payload']['meta']['company_ids']
        columns.extend([
            {'name': _('Selected months total'), 'expression_label': 'amount', 'figure_type': 'monetary',
             'scope': {'baseer_months': months, 'company_ids': column_company_ids}},
            {'name': _('% of collected sales'), 'expression_label': 'receipt_share',
             'figure_type': 'baseer_percentage', 'scope': {'baseer_months': months, 'company_ids': column_company_ids}},
        ])
        for index, note in enumerate(sorted(note_texts)):
            lines.append({'id': 'baseer-note-%s' % index, 'name': note,
                          'level': 1, 'parent_id': 'baseer-reconciliation', 'unfoldable': False,
                          'columns': [{'expression_label': column['expression_label'], 'value': None}
                                      for column in columns[1:]],
                          'meta': {'kind': 'baseer_note', 'baseer_role': 'note'}})
        if len(lines) > self._BASEER_ROW_LIMIT:
            raise UserError(_('The cash preview supports at most 1,000 report rows. Narrow the selection.'))
        first_snapshot = next(iter(snapshots.values()))
        totals = {}
        for key in first_snapshot['exact']['exact_totals']:
            totals[key] = (None if key in ('opening_cash_balance', 'closing_cash_balance') else
                           sum((snapshot['exact']['exact_totals'][key] for snapshot in snapshots.values()), ZERO))
        totals['sales_collections'] = selected_sales
        meta = dict(first_snapshot['payload']['meta'])
        meta.update({
            'baseer_months': months,
            'date_from': monthly_columns[0]['scope']['date_from'],
            'date_to': monthly_columns[-1]['scope']['date_to'],
            'exact_totals': {key: format(value, '.2f') if value is not None else None for key, value in totals.items()},
            'exact_rows': exact_rows,
            'month_exact_totals': {
                column['baseer_month']: {
                    key: format(value, '.2f')
                    for key, value in snapshots[column['expression_label']]['exact']['exact_totals'].items()
                } for column in monthly_columns
            },
            'exact_sales_collections': format(selected_sales, '.2f'),
            'sales_collection_policy': 'positive_evidenced_customer_collections_refunds_separate',
            'aggregate_records_used': budget['used'],
            'diagnostics': sorted(set().union(*(snapshot['payload']['meta']['diagnostics'] for snapshot in snapshots.values()))),
            'total_figure_types': {key: 'monetary' for key in totals},
        })
        payload = {'columns': columns, 'lines': lines,
                   'totals': {key: float(value) if value is not None else None for key, value in totals.items()},
                   'generated_at': fields.Datetime.now().isoformat(), 'meta': meta}
        return (payload, source_columns) if with_sources else payload

    @api.model
    def get_drilldown_action(self, options, line_id):
        """Recompute provenance under the caller's ACL; never accept source IDs.

        Category amounts are paid allocations of original documents. Their
        native invoices intentionally show full document values, not a false
        ledger-domain claim that those values equal the allocated report cell.
        """
        if not isinstance(line_id, str) or len(line_id) > 4096:
            raise UserError(_('Invalid cash report row.'))
        options = self.normalize_options(options or {})
        handler = self._authorized_report_handler(options)
        expression = options.get('baseer_drill_column', 'amount')
        if not isinstance(expression, str) or len(expression) > 64:
            raise UserError(_('Invalid cash report column.'))
        if 'baseer_months' in options:
            payload, source_columns = handler._compute_monthly(options, with_sources=True)
            sources = source_columns.get(expression, {})
        else:
            payload, sources = handler._compute_report(options, with_sources=True)
            if expression != 'amount':
                raise UserError(_('Invalid cash report column.'))
        if expression not in {column['expression_label'] for column in payload['columns'][1:]}:
            raise UserError(_('The requested column is outside the selected months.'))
        numeric_rows = {row['id'] for row in payload['lines']
                        if row.get('meta', {}).get('baseer_source_drilldown')}
        if line_id not in numeric_rows or line_id not in sources:
            raise UserError(_('This row has no numeric cash report source.'))
        report_row = next(row for row in payload['lines'] if row['id'] == line_id)
        cell = next((cell for cell in report_row['columns'] if cell['expression_label'] == expression), None)
        if not cell or cell.get('value') is None:
            raise UserError(_('This cell has no numeric cash report source.'))
        source = sources[line_id]
        ids = sorted(source['aml_ids'])
        # Each normal and opening scope is bounded at 10,000 above; closing
        # sources are their union. No unbounded provenance is serialized.
        if len(ids) > 2 * self._BASEER_LIMIT:
            raise UserError(_('Too many source records. Use a smaller reporting period.'))
        journal_items = handler.env['account.move.line'].browse(ids).exists()
        journal_items.check_access('read')
        company_id = payload['meta']['company_ids'][0]
        if any(line.company_id.id != company_id for line in journal_items):
            raise AccessError(_('Source records are outside the report company.'))
        if source['kind'] == 'documents':
            records = journal_items.move_id
            records.check_access('read')
            name = _('Original documents — report shows allocated paid portions; document totals may differ')
        else:
            records = journal_items
            name = (_('Cash journal items for reconciliation') if source.get('reconciliation')
                    else _('Cash journal items — report amounts may be allocated or exclude tax'))
        if source.get('sales_percentage'):
            name = _('Outgoing source documents — percentage of positive evidenced sales collections')
        action = {
            'type': 'ir.actions.act_window', 'name': name,
            'res_model': records._name, 'target': 'current',
            'domain': [('id', 'in', records.ids)],
            'context': {'allowed_company_ids': [company_id], 'create': False},
        }
        if len(records) == 1:
            action.update(view_mode='form', views=[(False, 'form')], res_id=records.id)
        else:
            action.update(view_mode='list,form', views=[(False, 'list'), (False, 'form')])
        return action


class BaseerCashReportRegistry(models.Model):
    _inherit = 'eh.account.dynamic.report'

    @api.private
    def _eh_render_result(self, options, result_format='json', use_cache=True,
                          result_builder=None, persist_payload=True):
        self.ensure_one()
        if self.code == 'baseer_cash_categories':
            use_cache = False
        return super()._eh_render_result(options, result_format=result_format, use_cache=use_cache,
                                         result_builder=result_builder, persist_payload=persist_payload)
