"""Shared compact presentation formatters."""


def format_voice_duration(seconds: int) -> str:
    """Format whole seconds with only meaningful duration components."""

    if seconds < 0:
        raise ValueError("seconds must not be negative")
    if seconds < 60:
        return f"{seconds} сек"

    total_minutes = seconds // 60
    total_hours, minutes = divmod(total_minutes, 60)
    if total_hours == 0:
        return f"{minutes} мин"

    days, hours = divmod(total_hours, 24)
    if days:
        parts = [f"{days} д"]
        if hours:
            parts.append(f"{hours} ч")
        if minutes:
            parts.append(f"{minutes:02d} мин")
        return " ".join(parts)
    return f"{hours} ч {minutes:02d} мин"
