"""Exchange a SimpleFIN setup token for an access URL, once.

    throughline-simplefin-claim --output ~/.config/throughline/simplefin-access-url

The setup token is read from standard input (or an interactive prompt that
does not echo), never from the command line, so it does not land in shell
history or the process list. The access URL it yields contains the
credentials to every connected account: it is written to the output file
with mode 0600 and never printed. A setup token can be claimed only once; if
someone else claimed it first, the Bridge refuses and you should disable that
token on the Bridge and make a new one.
"""

import argparse
import base64
import binascii
import getpass
import os
import sys
from pathlib import Path

import httpx

from . import split_access_url


def claim_url(setup_token):
    token = "".join(setup_token.split())
    try:
        url = base64.b64decode(token + "=" * (-len(token) % 4), validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise ValueError("that is not a SimpleFIN setup token (not base64)") from None
    if not url.startswith("https://"):
        raise ValueError("that is not a SimpleFIN setup token (no https claim URL inside)")
    return url


def claim(setup_token, transport=None):
    """The access URL for `setup_token` (POST to the claim URL it encodes)."""
    url = claim_url(setup_token)
    with httpx.Client(timeout=60, transport=transport) as http:
        response = http.post(url, headers={"Content-Length": "0"})
    if response.status_code == 403:
        raise ValueError(
            "the Bridge refused the claim (HTTP 403): the token was already claimed or "
            "has expired; if you did not claim it, disable it on the Bridge"
        )
    if response.is_error:
        raise ValueError(f"the claim failed (HTTP {response.status_code})")
    access = response.text.strip()
    split_access_url(access)  # Validates the shape without revealing it.
    return access


def write_secret(path, value):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    os.chmod(path, 0o600)
    return path


def main(argv=None, transport=None, stdin=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output",
        required=True,
        help="File to write the access URL to (mode 0600); point SIMPLEFIN_ACCESS_URL_FILE at it",
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing output file"
    )
    args = parser.parse_args(argv)
    target = Path(args.output).expanduser()
    if target.exists() and not args.force:
        parser.error(f"{target} exists; pass --force to replace it")
    stdin = stdin or sys.stdin
    if stdin.isatty():
        token = getpass.getpass("SimpleFIN setup token: ")
    else:
        token = stdin.read()
    try:
        access = claim(token, transport=transport)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"error: the claim request failed ({type(error).__name__})", file=sys.stderr)
        return 1
    write_secret(target, access)
    print(f"Access URL saved to {target} (mode 0600). Set SIMPLEFIN_ACCESS_URL_FILE={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
