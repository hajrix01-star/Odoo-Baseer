"""Read-only POS evidence at the native reconciliation edge, never a ledger.

Native sessions aggregate all payment methods into one sales/tax entry. Stop
at the matched session receivable: expanding every session counterpart once
per payment would count the same sale repeatedly. Ordinary POS stays on the
existing handler path; only this addon's approved one-order sessions qualify.
"""
from decimal import localcontext, ROUND_HALF_UP

from odoo import _, models
from odoo.exceptions import AccessError
from odoo.addons.baseer_cash_categories.models.cash_categories import (
    CENT, ZERO, allocate, money,
)


class PosSummaryCashReport(models.AbstractModel):
    _inherit = 'eh.account.dynamic.report.handler.baseer_cash_categories'

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
            cache[move.id] = False
            return False
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
        if (summary.state != 'approved' or summary.session_id != session
                or session.state != 'closed' or move.state != 'posted'
                or move.date > state['date_to'] or len(order) != 1
                or order.session_id != session or session.order_ids != order
                or order.baseer_summary_id != summary or order.source != 'baseer_summary'
                or order.company_id != summary.company_id
                or order.state != 'done' or order.account_move
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
        if (evidence.get('error') or amount <= ZERO
                or peer.account_id.account_type != 'asset_receivable'
                or money(peer.balance) <= ZERO):
            reason = evidence.get('error') or _(
                'POS summary cash has no supported positive receivable evidence; its full amount is retained.')
            return self._unknown(amount, peer, state, reason)
        with localcontext() as context:
            context.prec = 50
            net = (amount * evidence['net'] / evidence['gross']).quantize(CENT, rounding=ROUND_HALF_UP)
        return [{'gross': amount, 'net': net, 'source': peer.id,
                 'path': evidence['path'], 'unknown': False, 'sale_collection': True,
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
        pos_movements = [item for item in movements if item.get('baseer_pos_order_id')]
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
