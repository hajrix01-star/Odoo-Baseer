"""Decimal presentation boundaries and private native entry authorization."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from odoo.exceptions import AccessError, ValidationError

CENT = Decimal('0.01')
ZERO = Decimal('0.00')
MAX_AMOUNT = Decimal('999999999.99')
INTERNAL_TOKEN = object()  # Identity cannot be supplied by a JSON/RPC caller.
TOKEN_KEY = '_baseer_pos_summary_token'


def internal(record):
    return record.env.context.get(TOKEN_KEY) is INTERNAL_TOKEN


def money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def positive_amount(value, env, allow_zero=False):
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or (not allow_zero and number == 0) or number > MAX_AMOUNT or number != number.quantize(CENT):
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        message = env._('Enter a nonnegative amount with at most two decimals, no greater than 999999999.99.') if allow_zero else env._('Enter a positive amount with at most two decimals, no greater than 999999999.99.')
        raise ValidationError(message) from None
    return number.quantize(CENT)


def active_company(record, company):
    if len(company) != 1 or company.id not in record.env.user.company_ids.ids or company != record.env.company:
        raise AccessError(record.env._('Switch to the summary company before creating or changing this summary.'))
    company.check_access('read')
    if company.currency_id.name != 'SAR':
        raise ValidationError(record.env._('External POS summaries currently support SAR companies only.'))


def clean_context(record, company, authorized_internal=False):
    active_company(record, company)
    context = {'allowed_company_ids': company.ids, 'lang': record.env.lang, 'tz': 'Asia/Riyadh'}
    if authorized_internal:
        context[TOKEN_KEY] = INTERNAL_TOKEN
    return record.with_context(context)


def manager(record):
    if not record.env.user.has_group('point_of_sale.group_pos_manager') or not record.env.user.has_group('account.group_account_manager'):
        raise AccessError(record.env._('POS and accounting manager permissions are required to configure external summaries.'))


def lifecycle_manager(record):
    if not record.env.user.has_group('point_of_sale.group_pos_manager'):
        raise AccessError(record.env._('Only a POS manager can archive summaries or delete a draft day.'))


def native_quote(gross, tax, product, company):
    tax.check_access('read')
    if (not tax or not tax.active or tax.company_id != company or tax.type_tax_use != 'sale'
            or tax.amount_type != 'percent' or Decimal(str(tax.amount)) < 0
            or tax.include_base_amount or tax.has_negative_factor or tax.tax_exigibility != 'on_invoice'):
        raise ValidationError(company.env._('Configure one active simple sales percentage tax on invoice for this company.'))
    repartition = tax.invoice_repartition_line_ids.filtered(lambda r: r.repartition_type == 'tax')
    if sum((Decimal(str(r.factor_percent)) for r in repartition), ZERO) != Decimal('100') or any(not r.account_id for r in repartition):
        raise ValidationError(company.env._('Configure full sales tax repartition and its tax accounts before approving summaries.'))
    inverse = tax.with_context(force_price_include=True, round_base=False).compute_all(
        float(gross), currency=company.currency_id, quantity=1.0, product=product, rounding_method='round_globally')
    unit = gross if tax.price_include else Decimal(str(inverse['total_excluded']))
    forward = tax.compute_all(float(unit), currency=company.currency_id, quantity=1.0, product=product)
    if money(forward['total_included']) != gross:
        raise ValidationError(company.env._('The configured native tax cannot reproduce this gross amount exactly.'))
    net = money(forward['total_excluded'])
    return {'gross': gross, 'net': net, 'tax': gross - net, 'unit_price': unit}
