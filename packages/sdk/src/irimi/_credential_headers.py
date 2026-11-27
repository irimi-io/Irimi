from __future__ import annotations

from collections.abc import Sequence

from irimi._wire import JsonValue, escape_reserved_key, mark_redacted

CREDENTIAL_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "apikey",
        "x-goog-api-key",
        "x-auth-token",
        "x-auth-key",
        "x-amz-security-token",
        "ocp-apim-subscription-key",
        "x-functions-key",
        "private-token",
        "x-vault-token",
        "x-shopify-access-token",
        "dd-api-key",
    }
)


def replace_credential_values(headers: Sequence[tuple[str, str]]) -> dict[str, JsonValue]:
    return escape_reserved_key(
        {name: _record_values(name, values) for name, values in _group_by_name(headers).items()}
    )


def _group_by_name(headers: Sequence[tuple[str, str]]) -> dict[str, list[str]]:
    first_spellings: dict[str, str] = {}
    values_by_name: dict[str, list[str]] = {}
    for name, value in headers:
        spelling = first_spellings.setdefault(name.lower(), name)
        values_by_name.setdefault(spelling, []).append(value)
    return values_by_name


def _record_values(name: str, values: list[str]) -> JsonValue:
    if name.lower() in CREDENTIAL_HEADER_NAMES:
        return mark_redacted("credential")
    return ", ".join(values)
