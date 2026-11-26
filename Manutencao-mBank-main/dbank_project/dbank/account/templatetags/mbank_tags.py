from decimal import Decimal, InvalidOperation
from django import template

register = template.Library()

PT_MONTHS = [
    '', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
    'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro',
]


@register.filter(name='brl')
def format_brl(value):
    if value is None or value == '':
        return '0,00'
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    sign = '-' if dec < 0 else ''
    dec = abs(dec).quantize(Decimal('0.01'))
    formatted = f'{dec:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')
    return f'{sign}{formatted}'


@register.filter(name='pt_month')
def pt_month(month_number):
    try:
        idx = int(month_number)
        return PT_MONTHS[idx] if 1 <= idx <= 12 else ''
    except (ValueError, TypeError):
        return ''
