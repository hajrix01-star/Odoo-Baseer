"""Preserve pre-QA4 credit intent once; never change financial documents."""
import logging


def migrate(cr, version):
    if not version:
        return
    # This versioned migration runs only across the 1.2.0 upgrade boundary.
    # Existing empty methods explicitly meant credit before the new switch.
    # SQL intentionally preserves approved-row immutability and audit dates;
    # only this new declaration field is initialized, with no ORM posting.
    cr.execute('''
        UPDATE baseer_purchase_batch_line
           SET is_credit = (payment_method_line_id IS NULL)
    ''')
    logging.getLogger(__name__).info('Initialized explicit credit intent for %s existing purchase batch rows', cr.rowcount)
