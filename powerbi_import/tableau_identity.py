"""Shared identity handling for extracted Tableau identifiers."""


def unbracket_identifier(name):
    """Remove one outer Tableau identifier delimiter, preserving inner brackets."""
    if name.startswith('[') and name.endswith(']'):
        return name[1:-1]
    return name


def calculation_identity(calc):
    """Return a calculation's identifier and its distinct display caption."""
    name = calc.get('name', '')
    raw = unbracket_identifier(name)
    caption = calc.get('caption') or raw
    if caption == name:
        caption = raw
    return raw, caption
