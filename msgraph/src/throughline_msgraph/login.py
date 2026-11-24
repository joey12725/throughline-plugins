"""throughline-msgraph-login: sign in once with the device-code flow.

    MSGRAPH_CLIENT_ID=<your app's client id> throughline-msgraph-login
    throughline-msgraph-login --client-id <id> --tenant consumers --cache ~/.config/throughline/msgraph-token.json

Prints a URL and a code (on stderr), waits until you sign in in any browser,
and writes the token cache file with 0600 permissions. Nothing secret is
printed.
"""

import argparse
import os
import sys

import httpx

from .auth import DEFAULT_SCOPES, AuthError, device_login, save

DEFAULT_CACHE = "~/.config/throughline/msgraph-token.json"


def main(argv=None, http=None, sleep=None):
    parser = argparse.ArgumentParser(prog="throughline-msgraph-login", description=__doc__.split("\n\n")[0])
    parser.add_argument("--client-id", default=os.getenv("MSGRAPH_CLIENT_ID"), help="Application (client) id of your Entra app registration")
    parser.add_argument("--tenant", default=os.getenv("MSGRAPH_TENANT", "common"), help="common, consumers (Outlook.com), organizations, or a tenant id")
    parser.add_argument("--cache", default=os.getenv("MSGRAPH_TOKEN_CACHE", DEFAULT_CACHE), help="Token cache file to write")
    parser.add_argument("--scopes", default=os.getenv("MSGRAPH_SCOPES", DEFAULT_SCOPES), help="Space-separated scopes")
    args = parser.parse_args(argv)
    if not args.client_id:
        parser.error("--client-id or MSGRAPH_CLIENT_ID is required")
    client = http or httpx.Client(timeout=60)
    options = {"sleep": sleep} if sleep else {}
    try:
        cache = device_login(
            client, args.client_id, args.tenant, args.scopes, notify=lambda text: print(text, file=sys.stderr), **options
        )
    except AuthError as error:
        print(f"throughline-msgraph-login: {error}", file=sys.stderr)
        return 1
    finally:
        if http is None:
            client.close()
    save(args.cache, cache)
    print(f"Signed in; token cache written to {os.path.expanduser(args.cache)} (mode 0600).", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
