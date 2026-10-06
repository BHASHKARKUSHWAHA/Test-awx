"""Tiny YAML subset loader.

The installer has to read ``config/compatibility.yaml`` *before* it is allowed
to install anything - including ``python3-yaml``.  Rather than pretending the
config is JSON, this module parses the restricted YAML subset that ships with
this project:

* nested block mappings
* block sequences of scalars and of mappings
* flow sequences of scalars  (``["a", "b"]`` / ``[]``)
* scalars: str, int, float, bool, null
* ``#`` comments and blank lines

If PyYAML is importable it is used instead - see :func:`load`.  Anything this
parser cannot represent raises :class:`MiniYamlError` loudly instead of
silently returning half a config.
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple

__all__ = ["load", "loads", "MiniYamlError"]


class MiniYamlError(ValueError):
    """Raised when the input uses YAML features this parser does not support."""


_UNSUPPORTED = ("&", "*", "<<:", "---", "?", "|", ">")


def _scalar(raw: str) -> Any:
    text = raw.strip()
    if text == "" or text in ("null", "~", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE", "yes", "on"):
        return True
    if text in ("false", "False", "FALSE", "no", "off"):
        return False
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _split_flow(inner)]
    if text.startswith("{"):
        raise MiniYamlError("flow mappings are not supported: %r" % text)
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?", text):
        return float(text)
    return text


def _split_flow(inner: str) -> List[str]:
    parts, buf, quote = [], [], ""
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p for p in (p.strip() for p in parts) if p != ""]


def _strip_comment(line: str) -> str:
    out, quote = [], ""
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _tokenize(text: str) -> List[Tuple[int, str, int]]:
    rows: List[Tuple[int, str, int]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw.replace("\t", " " * 0) and raw.lstrip().startswith("\t"):
            raise MiniYamlError("tabs are not valid YAML indentation (line %d)" % lineno)
        line = _strip_comment(raw)
        if not line.strip():
            continue
        stripped = line.lstrip(" ")
        for bad in _UNSUPPORTED:
            if stripped.startswith(bad):
                raise MiniYamlError("unsupported YAML construct %r on line %d" % (bad, lineno))
        rows.append((len(line) - len(stripped), stripped, lineno))
    return rows


def _parse_block(rows: List[Tuple[int, str, int]], pos: int, indent: int) -> Tuple[Any, int]:
    if pos >= len(rows):
        return None, pos
    if rows[pos][1].startswith("- "):
        return _parse_sequence(rows, pos, indent)
    return _parse_mapping(rows, pos, indent)


def _parse_sequence(rows, pos, indent):
    items: List[Any] = []
    while pos < len(rows):
        cur_indent, content, lineno = rows[pos]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise MiniYamlError("unexpected indentation on line %d" % lineno)
        if not content.startswith("-"):
            break
        body = content[1:].strip()
        pos += 1
        if body == "":
            value, pos = _parse_block(rows, pos, indent + 1)
            items.append(value)
        elif ":" in body and not body.startswith(("\"", "'")) and _looks_like_key(body):
            # "- key: value" starts an inline mapping that may continue below.
            child_indent = cur_indent + 2
            sub_rows = [(child_indent, body, lineno)]
            while pos < len(rows) and rows[pos][0] >= child_indent:
                sub_rows.append(rows[pos])
                pos += 1
            value, _ = _parse_mapping(sub_rows, 0, child_indent)
            items.append(value)
        else:
            items.append(_scalar(body))
    return items, pos


def _looks_like_key(body: str) -> bool:
    head = body.split(":", 1)[0]
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-/ ]+", head))


def _parse_mapping(rows, pos, indent):
    mapping = {}
    while pos < len(rows):
        cur_indent, content, lineno = rows[pos]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise MiniYamlError("unexpected indentation on line %d" % lineno)
        if content.startswith("- "):
            break
        if ":" not in content:
            raise MiniYamlError("expected 'key: value' on line %d: %r" % (lineno, content))
        key_raw, _, rest = content.partition(":")
        key = _scalar(key_raw)
        rest = rest.strip()
        pos += 1
        if rest:
            mapping[key] = _scalar(rest)
            continue
        if pos < len(rows) and rows[pos][0] > cur_indent:
            value, pos = _parse_block(rows, pos, rows[pos][0])
            mapping[key] = value
        elif pos < len(rows) and rows[pos][0] == cur_indent and rows[pos][1].startswith("- "):
            value, pos = _parse_sequence(rows, pos, cur_indent)
            mapping[key] = value
        else:
            mapping[key] = None
    return mapping, pos


def loads(text: str) -> Any:
    """Parse ``text``; prefers PyYAML and falls back to the mini parser."""
    try:
        import yaml  # type: ignore
    except Exception:
        pass
    else:
        return yaml.safe_load(text)
    rows = _tokenize(text)
    if not rows:
        return None
    value, pos = _parse_block(rows, 0, rows[0][0])
    if pos != len(rows):
        raise MiniYamlError("could not parse document near line %d" % rows[pos][2])
    return value


def load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return loads(handle.read())
