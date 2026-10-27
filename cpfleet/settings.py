"""Edit settings.toml (flat KEY = value lines, as CircuitPython reads it) without exposing values."""
import re

_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def toml_string(value):
    """A TOML basic string."""
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def literal(value):
    """Integers stay integers (CircuitPython reads both); everything else becomes a quoted string."""
    return value if re.fullmatch(r"-?\d+", value) else toml_string(value)


def keys(text):
    return [m.group(1) for m in map(_LINE.match, text.splitlines()) if m]


def set_key(text, key, value_literal):
    lines, done = text.splitlines(), False
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(1) == key:
            lines[i] = "%s = %s" % (key, value_literal)
            done = True
    if not done:
        lines.append("%s = %s" % (key, value_literal))
    return "\n".join(lines) + "\n"


def unset_key(text, key):
    return "\n".join(l for l in text.splitlines() if not ((m := _LINE.match(l)) and m.group(1) == key)) + "\n"
