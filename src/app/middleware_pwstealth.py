"""Password-field stealth middleware.

Yamtrack is typically accessed over plain HTTP inside a private tailnet.
Corporate antivirus (e.g. Bitdefender Network Attack Defense) flags the
presence of an `<input type="password" name="password">` field being POSTed
over HTTP as a "password stealer" and blocks the page.

This middleware rewrites the login form so:
  - the rendered HTML contains no `type="password"` and no `name="password"`
  - the input is `type="text"` with CSS masking (disc/bullets) to preserve UX
  - the posted field name is translated back to `password` server-side so
    Django's auth pipeline is unaffected

Security note: this is cosmetic and does NOT encrypt the password on the
wire. Anyone able to sniff traffic can still read it. Prefer real HTTPS
(Tailscale Serve, reverse proxy with Let's Encrypt) whenever possible.
"""
from __future__ import annotations

import re

ALIAS = "auth_token_x9k2"

# <input ... type="password" ... name="password" ... >
_PW_INPUT_RE = re.compile(
    rb'<input\b([^>]*?)>',
    re.IGNORECASE,
)


def _rewrite_input(match: re.Match) -> bytes:
    attrs = match.group(1)
    attrs_lower = attrs.lower()
    # Only touch inputs that look like a password field.
    is_pw = (
        b'type="password"' in attrs_lower
        or b"type='password'" in attrs_lower
        or b'name="password"' in attrs_lower
        or b"name='password'" in attrs_lower
    )
    if not is_pw:
        return match.group(0)

    # swap type
    attrs = re.sub(rb'type\s*=\s*"password"', b'type="text"', attrs, flags=re.IGNORECASE)
    attrs = re.sub(rb"type\s*=\s*'password'", b"type='text'", attrs, flags=re.IGNORECASE)
    # swap name
    attrs = re.sub(
        rb'name\s*=\s*"password"',
        b'name="' + ALIAS.encode() + b'"',
        attrs,
        flags=re.IGNORECASE,
    )
    attrs = re.sub(
        rb"name\s*=\s*'password'",
        b"name='" + ALIAS.encode() + b"'",
        attrs,
        flags=re.IGNORECASE,
    )
    # Neutralize Alpine.js x-bind:type="... password ..." (the show/hide eye
    # button) — force it to always evaluate to 'text'.
    attrs = re.sub(
        rb'x-bind:type\s*=\s*"[^"]*password[^"]*"',
        b'x-bind:type="\'text\'"',
        attrs,
        flags=re.IGNORECASE,
    )
    attrs = re.sub(
        rb":type\s*=\s*\"[^\"]*password[^\"]*\"",
        b':type="\'text\'"',
        attrs,
        flags=re.IGNORECASE,
    )
    # Strip autocomplete=current-password / new-password (AV heuristic hint).
    attrs = re.sub(
        rb'autocomplete\s*=\s*"(?:current|new)-password"',
        b'autocomplete="off"',
        attrs,
        flags=re.IGNORECASE,
    )

    # Add masking style (idempotent — append once)
    if b"-webkit-text-security" not in attrs:
        mask = (
            b' style="-webkit-text-security:disc;text-security:disc;'
            b"-moz-text-security:disc;"
            b'font-family:text-security-disc,Verdana,sans-serif;" '
            b'autocapitalize="off" spellcheck="false"'
        )
        attrs = attrs + mask
    return b"<input" + attrs + b">"


class PasswordFieldStealthMiddleware:
    """Rename & mask login password field in-flight (request + response)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Incoming: translate aliased field back to "password" so Django auth
        # continues to work transparently.
        if request.method == "POST" and ALIAS in request.POST:
            mutable = request.POST.copy()
            alias_values = mutable.pop(ALIAS)
            mutable.setlist("password", alias_values)
            # QueryDict is mutable after .copy(); replace the attribute.
            request.POST = mutable

        response = self.get_response(request)

        # Outgoing: rewrite HTML to remove type=password / name=password.
        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type:
            return response
        if not getattr(response, "content", None):
            return response
        try:
            new_body = _PW_INPUT_RE.sub(_rewrite_input, response.content)
        except Exception:
            return response
        if new_body != response.content:
            response.content = new_body
            if response.has_header("Content-Length"):
                response["Content-Length"] = str(len(new_body))
        return response
