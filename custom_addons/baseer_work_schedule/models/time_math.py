"""Whole-minute schedule input; float conversion occurs only at native ORM boundary."""
import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP


def minutes(value, end=False):
    value = (value or '').strip()
    if not re.fullmatch(r'[0-9]{2}:[0-9]{2}', value):
        raise ValueError('format')
    hour, minute = map(int, value.split(':'))
    if minute > 59 or hour > 23 and not (end and hour == 24 and minute == 0):
        raise ValueError('range')
    return hour * 60 + minute


def hhmm(value):
    return '%02d:%02d' % divmod(value, 60)


def compile_periods(periods):
    """(days,start,end) -> native civil-day fragments and logical-day totals."""
    totals = defaultdict(int)
    fragments = []
    for days, start, end in periods:
        if not days or start == end:
            raise ValueError('empty')
        if end < start:
            end += 1440
        if not 0 < end - start <= 1440:
            raise ValueError('duration')
        for day in sorted(set(days)):
            if day not in range(7):
                raise ValueError('day')
            totals[day] += end - start
            fragments.append((day, start, min(end, 1440), day, start, end))
            if end > 1440:
                fragments.append(((day + 1) % 7, 0, end - 1440, day, start, end))
    fragments.sort()
    previous = {}
    for day, start, end, *_ in fragments:
        if start < previous.get(day, -1):
            raise ValueError('overlap')
        previous[day] = end
    if not fragments:
        raise ValueError('empty')
    return fragments, dict(totals)


def daily_average_label(totals):
    """Display the exact average to the nearest minute, without changing wages."""
    average_minutes = Decimal(sum(totals.values())) / Decimal(len(totals))
    return hhmm(int(average_minutes.quantize(Decimal('1'), rounding=ROUND_HALF_UP)))
