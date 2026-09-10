"""Posted Applications provenance for the register's operational projection."""
from collections import defaultdict
from decimal import Decimal

from odoo import _, api, models
from odoo.exceptions import UserError
from odoo.tools import SQL

from .financial_register import ZERO


def amount(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


class PlatformRegister(models.Model):
    _inherit = 'account.move'

    @api.model
    def _register_platform_origins(self, company, start, end):
        """Return visible sale rows and exact trace points; never replay posting."""
        self._register_require_access()
        payments = self.env['account.payment']
        payments.check_access('read')
        query = payments._search([('company_id', '=', company.id), ('pos_session_id', '!=', False)])
        self.env.flush_all()
        origins = self.env.execute_query(SQL('''
            SELECT p.id, p.move_id, s.move_id, pm.outstanding_account_id
              FROM (%s) permitted
              JOIN account_payment p ON p.id=permitted.id
              JOIN pos_payment_method pm ON pm.id=p.pos_payment_method_id
              JOIN baseer_pos_payment_category cat ON cat.id=pm.baseer_category_id
              JOIN pos_session s ON s.id=p.pos_session_id
              JOIN pos_config cfg ON cfg.id=s.config_id
             WHERE cat.kind='platform' AND s.state='closed'
               AND cfg.company_id=%s AND pm.company_id=%s AND cat.company_id=%s
        ''', query.select(SQL('account_payment.id')), company.id, company.id, company.id))
        if len(origins) > 10000:
            raise UserError(_('Too many Applications origins for this reporting period.'))
        ids = {ident for _payment, receipt, sale, _account in origins for ident in (receipt, sale) if ident}
        moves = self.search([('id', 'in', sorted(ids)), ('state', '=', 'posted'),
                             ('company_id', '=', company.id), ('date', '<=', end)])
        visible = {move.id: move for move in moves}
        lineage = {}
        frontier = moves
        for _depth in range(8):
            children = self.search([('reversed_entry_id', 'in', frontier.ids), ('state', '=', 'posted'),
                                    ('company_id', '=', company.id), ('date', '<=', end)])
            children = children.filtered(lambda move: move.id not in visible)
            if not children:
                break
            for child in children:
                visible[child.id] = child
                lineage.setdefault(child.reversed_entry_id.id, []).append(child.id)
            frontier = children
        else:
            raise UserError(_('Applications reversal history exceeds the supported reporting depth.'))
        lines = self.env['account.move.line'].search([('move_id', 'in', sorted(visible)),
            ('company_id', '=', company.id), ('parent_state', '=', 'posted')])
        by_move = defaultdict(list)
        for line in lines:
            by_move[line.move_id.id].append(line)

        def signature(ident):
            result = defaultdict(lambda: ZERO)
            for line in by_move[ident]:
                result[line.account_id.id] += amount(line.balance)
            return {key: value for key, value in result.items() if value}

        def family(ident):
            result = [(ident, 1)]
            for parent, sign in result:
                old = signature(parent)
                for child in lineage.get(parent, []):
                    current = signature(child)
                    if not old or current != {key: -value for key, value in old.items()}:
                        warnings.add(_('Applications reversal evidence is incomplete; no amount was estimated.'))
                        continue
                    result.append((child, -sign))
            return result

        rows, trace_points, warnings = {}, {}, set()
        platform_accounts = set()
        for _payment, receipt_id, sale_id, account_id in origins:
            platform_accounts.add(account_id)
            if receipt_id not in visible or sale_id not in visible:
                continue
            receipt_lines, sale_lines = by_move[receipt_id], by_move[sale_id]
            # Missing source lines must never become a partial balancing estimate.
            if (not receipt_lines or not sale_lines or sum((amount(l.balance) for l in receipt_lines), ZERO)
                    or sum((amount(l.balance) for l in sale_lines), ZERO)):
                warnings.add(_('Applications source evidence is incomplete; no amount was estimated.'))
                continue
            clearing = [l for l in receipt_lines if l.account_id.id == account_id and l.account_id.account_type == 'asset_current']
            other = [l for l in receipt_lines if l.account_id.id != account_id and amount(l.balance)]
            gross = sum((amount(l.balance) for l in clearing), ZERO)
            # Native POS can combine an equal sale and refund for one method
            # into a posted zero payment. Its complete zero ledger has nothing
            # to recognize or trace; it is not missing monetary evidence.
            if (clearing and not gross
                    and any(l.account_id.account_type == 'asset_receivable' for l in receipt_lines)
                    and all(not amount(l.balance) and not amount(l.amount_currency)
                            and (l.account_id.id == account_id or l.account_id.account_type == 'asset_receivable')
                            for l in receipt_lines)):
                continue
            if (not gross or not clearing or not other
                    or any(l.account_id.account_type != 'asset_receivable' for l in other)
                    or sum((amount(l.balance) for l in other), ZERO) != -gross):
                warnings.add(_('Applications source evidence is incomplete; no amount was estimated.'))
                continue
            sales_family = family(sale_id)
            signed_origins = {}
            for ident, sign in sales_family:
                signed_origins.setdefault(sign, ident)
                move = visible[ident]
                if start <= move.date <= end:
                    row = rows.setdefault(ident, {'receipts': ZERO, 'payments': ZERO})
                    row['receipts'] += gross * sign
            # The original collection evidence stays recognized even after its
            # own reversal. A reversed receipt qualifies only if the sale itself
            # has an exact native reversal with the same sign.
            for ident, sign in family(receipt_id):
                if sign not in signed_origins:
                    continue
                for line in by_move[ident]:
                    if line.account_id.id != account_id and amount(line.balance):
                        trace_points[line.id] = {'origin': signed_origins[sign], 'sign': 1 if gross * sign > 0 else -1}
        return {'rows': rows, 'trace_points': trace_points, 'platform_accounts': platform_accounts,
                'warnings': warnings}
