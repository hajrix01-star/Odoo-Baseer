"""Shared payroll boundaries: exact amounts, private transitions, employee locks."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from odoo import _
from odoo.exceptions import AccessError, ValidationError

INTERNAL = object()
ADVANCE_DISBURSE = object()
CENT = Decimal('0.01')

def decimal(value):
    try:
        result = Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        raise ValidationError(_('Enter a valid amount.'))
    if not result.is_finite():
        raise ValidationError(_('Enter a finite amount.'))
    return result

def money(value):
    return decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)

def validate_money(value):
    value = decimal(value)
    if value != money(value) or value < 0:
        raise ValidationError(_('Amounts must be non-negative with at most two decimal places.'))
    return value


def split_salary(gross, allowances, mode, daily_hours, work_days):
    """Legacy STANDARD_MONTHLY_V1 split, shared by preview and payroll posting."""
    gross, allowances = validate_money(gross), validate_money(allowances)
    if not gross and not allowances:
        return money(0), money(0), money(0)
    if allowances >= gross:
        raise ValidationError(_('Included allowances must be less than the total salary.'))
    if mode == 'inclusive':
        h, d = decimal(daily_hours), decimal(work_days)
        if not 0 < h <= 12 or not 1 <= d <= 31 or d != d.to_integral_value():
            raise ValidationError(_('Choose a work schedule of up to 12 daily hours and a salary basis of 1–31 days.'))
        k = (max(h - 8, Decimal(0)) * min(d, 26) + max(d - 26, Decimal(0)) * h) / Decimal(208)
        basic = money((gross - allowances * (1 + k)) / (1 + Decimal('1.5') * k))
    else:
        basic = money(gross - allowances)
    overtime = money(gross - basic - allowances)
    if basic <= 0 or overtime < 0:
        raise ValidationError(_('The total salary is too low for these allowances and working hours.'))
    return basic, overtime, money(allowances)

def require_manager(record):
    record.check_access('write')
    advance_gateway = (record.env.su and record._name == 'baseer.hr.loan'
                       and record.env.context.get('baseer_advance_disburse') is ADVANCE_DISBURSE)
    if not advance_gateway and not record.env.user.has_group('om_hr_payroll.group_hr_payroll_manager'):
        raise AccessError(_('Payroll manager access is required.'))
    companies = record.mapped('company_id') if 'company_id' in record._fields else record.env.company
    if any(c != record.env.company for c in companies):
        raise AccessError(_('Switch to the company that owns this record.'))

def lock_employee(env, employee_id, company_id):
    # Advisory key is stable across payroll, advances, repayments and allocations.
    env.cr.execute('SELECT pg_advisory_xact_lock(%s, %s)', (int(company_id), int(employee_id)))
