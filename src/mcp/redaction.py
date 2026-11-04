"""Bounded output scrubbing at the MCP boundary, including stored receipts."""
import json
import os
import re
from typing import Any

SECRET_KEY = re.compile(r"(?:password|passwd|secret|cookie|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|bearer|private[_-]?key)", re.I)
TOKEN_KEY = re.compile(r"(?:^|[_-])token$", re.I)
SECRET_ENV = re.compile(r"(?:TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|PRIVATE_KEY|COOKIE)")
QUOTED_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
HEADER_PART = re.compile(QUOTED_VALUE + r"|[\[{]")
HEADER_TAIL = re.compile(r"[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*")
HEADER_VALUE = re.compile(
    r'''(?im)(?<![\w-])(["']?(?:authorization|proxy-authorization|cookie|set-cookie)["']?\s*[:=]\s*)'''
    + r"(?:" + QUOTED_VALUE + r"|[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*)"
)
FIELD_PREFIX = r'''(?P<prefix>(?<![\w-])["']?(?P<key>[A-Za-z_][\w.-]*)["']?\s*[:=]\s*)'''
TEXT_START = re.compile(FIELD_PREFIX + r"|[\[{]")
ASSIGN_VALUE = re.compile(
    FIELD_PREFIX
    + r"(?P<value>" + QUOTED_VALUE + r'''|[^\s,;{}\[\]"']+)'''
)
AUTH_VALUE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*")
USERINFO = re.compile(r"(https?|socks5h?)://[^/@\s]+:[^/@\s]+@", re.I)
MAX_OUTPUT_BYTES = 120000


def credential_field(key: str, value: Any) -> bool:
    # Namespaced/camelCase credentials use the same policy in objects and logs.
    # Token counts, usage groups and tokenizer/prompt settings are not secrets.
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return bool(SECRET_KEY.search(normalized) or (TOKEN_KEY.search(normalized) and isinstance(value, str)))


def redact(value: Any) -> Any:
    secrets = sorted((v for k, v in os.environ.items() if SECRET_ENV.search(k) and len(v) >= 8), key=len, reverse=True)
    decoder = json.JSONDecoder()

    def plain(text):
        for secret in secrets:
            text = text.replace(secret, "[redacted]")
        text = HEADER_VALUE.sub(lambda m: m.group(1) + "[redacted]", text)

        def assignment(match):
            raw = match.group("value")
            try:
                field_value = json.loads(raw)
            except (ValueError, RecursionError):
                field_value = raw
            if not credential_field(match.group("key"), field_value):
                return match.group(0)
            # Retain quotes so another pass cannot corrupt a serialized receipt.
            replacement = '"[redacted]"' if raw.startswith('"') else "'[redacted]'" if raw.startswith("'") else "[redacted]"
            return match.group("prefix") + replacement

        text = ASSIGN_VALUE.sub(assignment, text)
        text = AUTH_VALUE.sub(lambda m: m.group(1) + " [redacted]", text)
        return USERINFO.sub(r"\1://[redacted]@", text)

    def structured(raw, decoded, depth):
        cleaned = visit(decoded, depth + 1)
        # Preserve untouched JSON formatting and make receipt re-scrubbing stable.
        return raw if cleaned == decoded else json.dumps(cleaned, ensure_ascii=False, allow_nan=False)

    def header_end(text, start, end):
        # A header can contain a quoted or JSON value crossing an unfolded
        # newline. Complete that value before consuming the rest of its header,
        # but do not swallow the following independent log event.
        tail = HEADER_TAIL.match(text, end)
        if tail:
            end = tail.end()
        while part := HEADER_PART.search(text, start):
            if part.start() >= end:
                break
            start = part.end()
            if part.group() in ("{", "["):
                try:
                    _, start = decoder.raw_decode(text, part.start())
                except ValueError:
                    continue
            if start > end:
                tail = HEADER_TAIL.match(text, start)
                end = tail.end() if tail else start
        return end

    def visit(obj, depth=0):
        if depth > 20:
            return "[depth-limited]"
        if isinstance(obj, dict):
            return {
                visit(str(k), depth + 1): "[redacted]"
                if credential_field(str(k), v) else visit(v, depth + 1)
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [visit(v, depth + 1) for v in obj]
        if isinstance(obj, str):
            # Whole environment credentials take precedence over parsing: a
            # JSON-shaped cookie may have no sensitive keys of its own. Apply
            # this on every visit, including decoded strings and dictionary keys.
            for secret in secrets:
                obj = obj.replace(secret, "[redacted]")
            # Decode complete JSON before looking for credential fields. This
            # handles escaped keys, quoted JSON and nested serialized log data.
            if obj.lstrip().startswith(("{", "[", '"')):
                try:
                    decoded = json.loads(obj)
                except ValueError:
                    pass
                except RecursionError:
                    return "[depth-limited]"
                else:
                    return structured(obj, decoded, depth)
            # Some loggers emit JSON-escaped text without its outer quotes.
            # Decode only valid JSON string content, not arbitrary backslashes.
            if '\\"' in obj:
                try:
                    decoded = json.loads('"' + obj + '"')
                except ValueError:
                    pass
                else:
                    cleaned = visit(decoded, depth + 1)
                    return obj if cleaned == decoded else json.dumps(cleaned, ensure_ascii=False)[1:-1]
            # Scan fields and JSON in source order. An outer credential owns its
            # complete quoted/structured value, even if JSON starts inside it.
            # Conversely, JSON encountered first is scrubbed structurally, so a
            # nested header regex cannot eat its surrounding object/safe fields.
            pieces, start, position = [], 0, 0
            while match := TEXT_START.search(obj, position):
                position = match.start()
                if match.group("key"):
                    header = HEADER_VALUE.match(obj, position)
                    scalar = ASSIGN_VALUE.match(obj, position)
                    if header:
                        try:
                            end = header_end(obj, match.end(), header.end())
                        except RecursionError:
                            return "[depth-limited]"
                        replacement = "[redacted]"
                    else:
                        if scalar:
                            raw, end = scalar.group("value"), scalar.end()
                            try:
                                decoded = json.loads(raw)
                            except (ValueError, RecursionError):
                                decoded = raw
                        else:
                            try:
                                decoded, end = decoder.raw_decode(obj, match.end())
                            except ValueError:
                                position = match.end()
                                continue
                            except RecursionError:
                                return "[depth-limited]"
                            raw = obj[match.end():end]
                        if not credential_field(match.group("key"), decoded):
                            position = match.end()
                            continue
                        replacement = '"[redacted]"' if raw.startswith('"') else "'[redacted]'" if raw.startswith("'") else "[redacted]"
                    pieces.extend((plain(obj[start:position]), match.group("prefix") + replacement))
                    start = position = end
                    continue
                try:
                    decoded, end = decoder.raw_decode(obj, position)
                except ValueError:
                    position += 1
                    continue
                except RecursionError:
                    return "[depth-limited]"
                pieces.extend((plain(obj[start:position]), structured(obj[position:end], decoded, depth)))
                start = position = end
            pieces.append(plain(obj[start:]))
            return "".join(pieces)
        return obj
    return visit(value)


def safe_payload(payload: Any) -> tuple[Any, bool]:
    value = redact(payload)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return {"error": "output_limit_exceeded", "hint": "Reduce page size, log limit or export size."}, True
    return value, False
