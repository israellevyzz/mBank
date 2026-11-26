def compute_initials(user):
    if not user or not user.is_authenticated:
        return ''
    raw = (getattr(user, 'name', '') or getattr(user, 'username', '') or '').strip()
    parts = [p for p in raw.split() if p]
    if not parts:
        return 'MB'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def mbank_shell(request):
    user = getattr(request, 'user', None)
    if user and user.is_authenticated:
        display_name = (user.name or user.username).strip()
        first_name = display_name.split()[0] if display_name else user.username
        initials = compute_initials(user)
    else:
        display_name = ''
        first_name = ''
        initials = ''
    return {
        'user_display_name': display_name,
        'user_first_name': first_name,
        'user_initials': initials,
    }
