"""Read-only POS evidence at the native reconciliation edge, never a ledger.

Native sessions aggregate all payment methods into one sales/tax entry. Stop
at the matched session receivable: expanding every session counterpart once
per payment would count the same sale repeatedly. Native order/payment totals
and their posted sales family provide the same boundary for ordinary sessions.
"""
from decimal import localcontext, ROUND_HALF_UP

from odoo import _, models
from odoo.exceptions import AccessError
from odoo.addons.baseer_cash_categories.models.cash_categories import (
    CENT, ZERO, allocate, money,
)


class PosSummaryCashReport(models.AbstractModel):
    _inherit = 'eh.account.dynamic.report.handler.baseer_cash_categories'

    def _baseer_pos_exact_reversal(self, original, reversal, state):
        cache = state.setdefault('baseer_pos_reversal_evidence', {})
        key = (original.id, reversal.id)
        if key in cache:
            return cache[key]
        ledger = (original | reversal).line_ids
        ledger.check_access('read')
        state['visits'] += len(ledger)
        self._check_budget(state)
        balances = {}
        for line in ledger:
            line.check_access('read')
            row_key = (line.account_id.id, line.partner_id.id, line.currency_id.id)
            values = balances.setdefault(row_key, [ZERO, ZERO])
            values[0] += money(line.balance)
            values[1] += money(line.amount_currency)
        result = bool(original.line_ids and reversal.line_ids) and not any(any(values) for values in balances.values())
        cache[key] = result
        return result

    def _baseer_native_pos_evidence(self, session, state):
        cache = state.setdefault('baseer_native_pos_evidence', {})
        if session.id in cache:
            return cache[session.id]
        error = {'error': _('Native POS accounting evidence is incomplete; its full cash amount is retained.')}
        cache[session.id] = error
        session.check_access('read')
        orders, payments = session.order_ids, session.order_ids.payment_ids
        order_lines = orders.lines
        for records in (orders, payments, order_lines):
            records.check_access('read')
        state['visits'] += len(orders) + len(payments) + len(order_lines) + 1
        self._check_budget(state)
        if (session.company_id.id != state['company_id'] or any(order.company_id != session.company_id for order in orders)):
            raise AccessError(_('POS evidence is outside the report company.'))
        if (session.state != 'closed' or session.move_id.state != 'posted'
                or session.move_id.date > state['date_to'] or not orders
                or any(order.state != 'done' or order.currency_id != session.company_id.currency_id
                       or order.session_id != session for order in orders)):
            return error
        sales_moves = session.move_id | orders.account_move | orders.reversed_move_ids
        sales_moves = sales_moves.filtered(lambda move: move.state == 'posted' and move.date <= state['date_to'])
        ledger = sales_moves.line_ids
        sales_moves.check_access('read')
        ledger.check_access('read')
        state['visits'] += len(ledger)
        self._check_budget(state)
        if any(move.company_id != session.company_id for move in sales_moves):
            raise AccessError(_('POS accounting evidence is outside the report company.'))
        gross = net = tax = ZERO
        parts = []
        for order in orders:
            order_gross = money(order.amount_total)
            # Native Odoo stores refund line subtotals in the order's refund
            # orientation (_compute_amount_line_all multiplies qty by -1).
            # Restore the ledger sign before comparing the native proof.
            sign = -1 if order.is_refund else 1
            order_net = sign * sum((money(line.price_subtotal) for line in order.lines), ZERO)
            order_tax = money(order.amount_tax)
            if (sign * sum((money(line.price_subtotal_incl) for line in order.lines), ZERO) != order_gross
                    or order_net + order_tax != order_gross
                    or sum((money(payment.amount) for payment in order.payment_ids), ZERO) != order_gross):
                return error
            if not order_gross and (order_net or order_tax or any(money(payment.amount) for payment in order.payment_ids)):
                return error
            gross, net, tax = gross + order_gross, net + order_net, tax + order_tax
            for payment in order.payment_ids:
                if payment.session_id != session or payment.payment_method_id.company_id != session.company_id:
                    return error
                paid = money(payment.amount)
                if paid:
                    with localcontext() as context:
                        context.prec = 50
                        paid_net = paid * order_net / order_gross
                    parts.append({'method_id': payment.payment_method_id.id,
                        'partner_id': order.partner_id.commercial_partner_id.id,
                        'pay_later': payment.payment_method_id.type == 'pay_later',
                        'gross': paid, 'net': paid_net})
        income = ledger.filtered(lambda line: line.account_id.account_type in ('income', 'income_other'))
        taxes = ledger.filtered('tax_line_id')
        if (not income or -sum((money(line.balance) for line in income), ZERO) != net
                or -sum((money(line.balance) for line in taxes), ZERO) != tax
                or any(sum((money(line.balance) for line in move.line_ids), ZERO) for move in sales_moves)):
            return error
        evidence = {'native': True, 'session_id': session.id, 'gross': gross, 'net': net,
            'parts': parts, 'path': self._path(income[:1]) if len(income.account_id) == 1
            else [('native-pos-sales', _('Point of Sale sales'))]}
        cache[session.id] = evidence
        return evidence

    def _baseer_native_pos_fragment(self, peer, amount, evidence, state, method_id=None):
        if evidence.get('error'):
            return self._unknown(amount, peer, state, evidence['error'])
        parts = [part for part in evidence['parts'] if part['method_id'] == method_id] if method_id else [
            part for part in evidence['parts'] if part['pay_later'] and
            part['partner_id'] == peer.partner_id.commercial_partner_id.id]
        gross = sum((part['gross'] for part in parts), ZERO)
        if not gross or not parts:
            return self._unknown(amount, peer, state, _('The POS payment allocation has no matching order evidence.'))
        with localcontext() as context:
            context.prec = 50
            net = (amount * sum((part['net'] for part in parts), ZERO) / gross).quantize(CENT, rounding=ROUND_HALF_UP)
        return [{'gross': amount, 'net': net, 'source': peer.id, 'path': evidence['path'],
                 'unknown': False, 'sale_collection': amount > ZERO,
                 'baseer_native_pos_session_id': evidence['session_id']}]

    def _baseer_pos_receipt_boundary(self, line, amount, state):
        """Read the original receipt, even when its current match is a reversal."""
        if line.account_id.account_type != 'asset_receivable' or not self.env['pos.session'].has_access('read'):
            return None
        original = line.move_id
        for _depth in range(8):
            parent = original.reversed_entry_id
            if not parent:
                break
            parent.check_access('read')
            if parent.state != 'posted' or not self._baseer_pos_exact_reversal(parent, original, state):
                return None
            original = parent
        else:
            return None
        payment, statement = original.origin_payment_id, original.statement_line_id
        session = payment.pos_session_id or statement.pos_session_id
        if not session:
            return None
        session.check_access('read')
        if session.company_id.id != state['company_id'] or original.company_id != session.company_id:
            raise AccessError(_('POS receipt evidence is outside the report company.'))
        if original.state != 'posted' or original.date > state['date_to']:
            return None
        examined = state.setdefault('baseer_pos_receipts_examined', set())
        if original.id not in examined:
            state['visits'] += len(original.line_ids)
            self._check_budget(state)
            examined.add(original.id)
        receivable = original.line_ids.filtered(lambda item: item.account_id.account_type == 'asset_receivable' and money(item.balance))
        receivable.check_access('read')
        if len(receivable) != 1:
            return None
        if session.baseer_summary_id:
            peer = session.move_id.line_ids.filtered(lambda item: item.account_id.account_type == 'asset_receivable' and money(item.balance) > ZERO)[:1]
            if not peer:
                return None
            evidence = self._baseer_pos_evidence(peer, state)
            if not evidence:
                return None
            return self._baseer_pos_fragment(peer, amount, evidence, state)
        method = payment.pos_payment_method_id if payment else session.payment_method_ids.filtered(
            lambda item: item.journal_id == original.journal_id and item.is_cash_count)
        if len(method) != 1:
            return None
        method.check_access('read')
        receipt_cache = state.setdefault('baseer_native_receipt_uniqueness', {})
        if original.id not in receipt_cache:
            if payment:
                candidates = session.bank_payment_ids.filtered(lambda item:
                    item.pos_payment_method_id == method and item.move_id.state == 'posted'
                    and item.move_id.date <= state['date_to'] and not item.move_id.reversed_entry_id)
                candidates.check_access('read')
                unique = candidates == payment
                state['visits'] += len(candidates)
            else:
                candidates = session.statement_line_ids.filtered(lambda item:
                    item.journal_id == original.journal_id and item.move_id.state == 'posted'
                    and item.date <= state['date_to'] and not item.move_id.reversed_entry_id)
                candidates.check_access('read')
                candidate_lines = candidates.move_id.line_ids
                candidate_lines.check_access('read')
                state['visits'] += len(candidates) + len(candidate_lines)
                candidate_moves = candidate_lines.filtered(lambda item:
                    item.account_id.account_type == 'asset_receivable' and money(item.balance)).move_id
                unique = candidate_moves == original
            self._check_budget(state)
            receipt_cache[original.id] = unique
        if not receipt_cache[original.id]:
            return self._unknown(amount, line, state, _('The POS receipt has ambiguous native payment evidence.'))
        evidence = self._baseer_native_pos_evidence(session, state)
        if evidence.get('error'):
            return self._unknown(amount, line, state, evidence['error'])
        wanted = sum((part['gross'] for part in evidence['parts'] if part['method_id'] == method.id), ZERO)
        if wanted != -money(receivable.balance):
            return self._unknown(amount, line, state, _('The POS receipt does not match its native payment allocations.'))
        return self._baseer_native_pos_fragment(line, amount, evidence, state, method.id)

    def _baseer_pos_evidence(self, peer, state):
        """False means ordinary accounting; an error means owned but unproven."""
        cache = state.setdefault('baseer_pos_evidence', {})
        move = peer.move_id
        if move.id in cache:
            return cache[move.id]
        move.check_access('read')
        if not self.env['pos.session'].has_access('read'):
            # Reading this one2many itself searches the protected POS model.
            # The accounting-origin guard in _trace handles POS cash without
            # requiring this relation or weakening ordinary accounting ACLs.
            cache[move.id] = False
            return False
        sessions = move.pos_session_ids
        self._consume_budget(state.get('budget'), len(sessions))
        if not sessions:
            cache[move.id] = False
            return False
        if not sessions.has_access('read'):
            # Accounting access must not imply access to POS operations. The
            # visible ledger amount is still useful without private POS data.
            cache[move.id] = {'error': _('POS source details are not accessible; the full cash amount is retained without inferring tax.')}
            return cache[move.id]
        owned = sessions.filtered('baseer_summary_id')
        if not owned:
            result = self._baseer_native_pos_evidence(sessions, state) if len(sessions) == 1 else False
            cache[move.id] = result
            return result
        error = {'error': _('POS summary accounting evidence is incomplete; its full cash amount is retained.')}
        cache[move.id] = error
        if len(sessions) != 1 or len(owned) != 1:
            return error
        session = owned
        summary = session.baseer_summary_id
        summary.check_access('read')
        order = summary.order_id
        order.check_access('read')
        session.order_ids.check_access('read')
        if (summary.company_id.id != state['company_id']
                or session.company_id.id != state['company_id']
                or move.company_id.id != state['company_id']):
            raise AccessError(_('POS summary evidence is outside the report company.'))
        historical = summary.state == 'cancelled' and summary.reversal_move_ids
        if historical:
            originals = summary._native_moves()
            historical = (set(summary.reversal_move_ids.reversed_entry_id.ids) == set(originals.ids)
                and all(reversal.state == 'posted' and self._baseer_pos_exact_reversal(reversal.reversed_entry_id, reversal, state)
                        for reversal in summary.reversal_move_ids))
        if (summary.state != 'approved' and not historical or summary.session_id != session
                or session.state != 'closed' or move.state != 'posted'
                or move.date > state['date_to'] or len(order) != 1
                or order.session_id != session or session.order_ids != order
                or order.baseer_summary_id != summary or order.source != 'baseer_summary'
                or order.company_id != summary.company_id
                or order.state != ('cancel' if historical else 'done') or order.account_move
                or order.currency_id != summary.company_id.currency_id
                or order.currency_id.name != 'SAR'):
            return error
        order_lines = order.lines
        ledger_lines = move.line_ids
        order_lines.check_access('read')
        ledger_lines.check_access('read')
        state['visits'] += len(order_lines) + len(ledger_lines) + 2
        self._check_budget(state)
        if len(order_lines) != 1 or order_lines.qty <= 0:
            return error
        gross = money(order.amount_total)
        net = money(order_lines.price_subtotal)
        tax = money(order.amount_tax)
        income = ledger_lines.filtered(lambda row: row.account_id.account_type in ('income', 'income_other'))
        taxes = ledger_lines.filtered('tax_line_id')
        if (gross <= ZERO or money(order_lines.price_subtotal_incl) != gross
                or net + tax != gross
                or -sum((money(row.balance) for row in income), ZERO) != net
                or -sum((money(row.balance) for row in taxes), ZERO) != tax
                or sum((money(row.balance) for row in ledger_lines), ZERO) != ZERO):
            return error
        # The shared path helper expects an accounting line (including its
        # account fallback), not a POS line. Income AMLs are the posted proof.
        if not income:
            return error
        evidence = {'order_id': order.id, 'gross': gross, 'net': net,
                    'path': self._path(income.sorted('id')[:1])}
        cache[move.id] = evidence
        return evidence

    def _baseer_pos_fragment(self, peer, amount, evidence, state):
        if evidence.get('native'):
            return self._baseer_native_pos_fragment(peer, amount, evidence, state)
        if (evidence.get('error')
                or peer.account_id.account_type != 'asset_receivable'
                or money(peer.balance) <= ZERO):
            reason = evidence.get('error') or _(
                'POS summary cash has no supported positive receivable evidence; its full amount is retained.')
            return self._unknown(amount, peer, state, reason)
        with localcontext() as context:
            context.prec = 50
            net = (amount * evidence['net'] / evidence['gross']).quantize(CENT, rounding=ROUND_HALF_UP)
        return [{'gross': amount, 'net': net, 'source': peer.id,
                           'path': evidence['path'], 'unknown': False, 'sale_collection': amount > ZERO,
                 'baseer_pos_order_id': evidence['order_id']}]

    def _trace(self, line, amount, state, visited=frozenset(), depth=0):
        if (not amount or line.id in visited or depth > 6
                or line.account_id.account_type == 'asset_cash'
                or line.move_id.is_invoice(include_receipts=True)):
            return super()._trace(line, amount, state, visited, depth)
        line.check_access('read')
        if line.company_id.id != state['company_id']:
            raise AccessError(_('Cross-company reconciliation is not permitted.'))
        if line.move_id.state != 'posted' or line.date > state['date_to']:
            return super()._trace(line, amount, state, visited, depth)
        boundary = self._baseer_pos_receipt_boundary(line, amount, state)
        if boundary is not None:
            state['visits'] += 1
            self._check_budget(state)
            return boundary
        matches = (line.matched_debit_ids | line.matched_credit_ids).sorted('id')
        self._consume_budget(state.get('budget'), len(matches))
        if not self.env['pos.session'].has_access('read'):
            native_pos_origin = (line.move_id.statement_line_id.pos_session_id
                                 or line.move_id.origin_payment_id.pos_session_id)
            invoice_peers = (matches.debit_move_id | matches.credit_move_id).move_id.filtered(
                lambda move: move.is_invoice(include_receipts=True))
            if native_pos_origin and not invoice_peers:
                state['visits'] += 1
                self._check_budget(state)
                return self._unknown(amount, line, state, _(
                    'POS source details are not accessible; the full cash amount is retained without inferring tax.'))
        edges = []
        for match in matches:
            match.check_access('read')
            peer = match.credit_move_id if match.debit_move_id == line else match.debit_move_id
            peer.check_access('read')
            if peer.company_id.id != state['company_id']:
                raise AccessError(_('Cross-company reconciliation is not permitted.'))
            evidence = self._baseer_pos_evidence(peer, state)
            edges.append((match, peer, evidence))
        if not any(evidence for _, _, evidence in edges):
            return super()._trace(line, amount, state, visited, depth)
        state['visits'] += 1
        self._check_budget(state)
        visited = visited | {line.id}
        balance = abs(money(line.balance))
        if not balance:
            return self._unknown(amount, line, state, _('POS settlement has no matching receivable balance.'))
        result, used = [], ZERO
        for match, peer, evidence in edges:
            portion = (amount * money(match.amount) / balance).quantize(CENT, rounding=ROUND_HALF_UP)
            if abs(used + portion) > abs(amount):
                portion = amount - used
            if not portion:
                continue
            used += portion
            if peer.id in visited or peer.move_id.state != 'posted' or peer.date > state['date_to']:
                result.extend(self._unknown(portion, line, state, _('Unproven POS settlement chain.')))
            elif evidence:
                result.extend(self._baseer_pos_fragment(peer, portion, evidence, state))
            elif peer.move_id.is_invoice(include_receipts=True):
                result.extend(self._trace(peer, portion, state, visited, depth + 1))
            else:
                boundary = self._baseer_cash_source_boundary(peer, portion, state)
                if boundary is not None:
                    result.extend(boundary)
                    continue
                # Preserve the base handler's traversal for other matches in
                # a mixed settlement instead of reprocessing the POS edge.
                others = peer.move_id.line_ids.filtered(lambda row: row.id != peer.id and row.balance).sorted('id')
                weights = [-money(row.balance) for row in others]
                if not weights or sum(weights, ZERO) != money(peer.balance):
                    result.extend(self._unknown(portion, line, state, _('Unbalanced counterpart allocation.')))
                else:
                    for other, share in zip(others, allocate(portion, weights)):
                        result.extend(self._trace(other, share, state, visited | {peer.id}, depth + 1))
        if amount != used:
            result.extend(self._unknown(amount - used, line, state, _('Unreconciled cash counterpart.')))
        return result

    def _payload(self, movements, opening, change, state, options, company, date_from, date_to,
                 with_sources=False, internal=None):
        pos_movements = [item for item in movements if item.get('baseer_pos_order_id') or item.get('baseer_native_pos_session_id')]
        if pos_movements:
            state['diagnostics'].add(_(
                'Approved POS summaries count only reconciled cash or bank collections; platform clearing is not cash.'))
            state['diagnostics'].add(_(
                'POS tax portions are rounded for each actual receipt; their sum can differ by a cent from the full source tax.'))
        result = super()._payload(movements, opening, change, state, options, company, date_from, date_to,
                                  with_sources=with_sources, internal=internal)
        payload = result[0] if with_sources else result
        if pos_movements:
            for row in payload['lines']:
                if row['id'] == 'baseer-note-1':
                    row['name'] = _('Only tax evidenced by posted invoices or approved POS summaries is excluded; unknown tax retains the full cash amount.')
            payload['meta']['baseer_pos_summary_evidence'] = True
            if with_sources:
                source = result[1].get('baseer-total-excluded_tax_bridge')
                if source is not None:
                    source['aml_ids'].update(item['source'] for item in pos_movements if item['gross'] != item['net'])
        return result
