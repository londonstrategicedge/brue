#!/usr/bin/env python3
"""Auth driver for Meridian Markets: plays the human + the browser.

Conformance hands this script the `auth.begin` url as its last argument. It
does exactly what a person at a real browser would do, nothing more:

  1. GET the url                    (the broker's login page loads)
  2. POST username + password       (the Sign in button)
  3. follow the 302                 (the browser lands on the adapter's
                                     loopback listener, which finishes the
                                     token exchange itself)

Credentials come only from MERIDIAN_USERNAME / MERIDIAN_PASSWORD; both
must be set. Exit 0 only if the loopback answered 200, i.e. the ADAPTER said
the login finished; a wrong password never gets that far because the venue
re-shows the form with a 401 and no code.

This script talks only to the venue and to 127.0.0.1. It never sees a
token: the code rides the redirect straight into the adapter, same as a
real browser.
"""
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

url = sys.argv[-1]
user = os.environ.get("MERIDIAN_USERNAME", "")
pwd = os.environ.get("MERIDIAN_PASSWORD", "")
if not user or not pwd:
    print("driver: MERIDIAN_USERNAME and MERIDIAN_PASSWORD must both be set",
          file=sys.stderr)
    sys.exit(2)

page = urllib.request.urlopen(url, timeout=15).read().decode("utf-8", "replace")
if "password" not in page.lower():
    print("driver: the url did not serve a login form", file=sys.stderr)
    sys.exit(2)

# The Sign in POST: the OAuth request params ride along as the form's hidden
# fields, which mirror the url's own query string exactly.
base = url.split("/oauth/authorize")[0]
q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
form = urllib.parse.urlencode({**q, "username": user, "password": pwd}).encode()
try:
    with urllib.request.urlopen(
            urllib.request.Request(base + "/oauth/authorize", data=form),
            timeout=15) as resp:
        landed = resp.geturl()
        status = resp.status
        body = resp.read().decode("utf-8", "replace")
except urllib.error.HTTPError as e:
    print(f"driver: login refused (HTTP {e.code})", file=sys.stderr)
    sys.exit(1)

print(f"driver: landed on {landed} ({status})")
if status == 200 and "127.0.0.1" in landed:
    sys.exit(0)
print(f"driver: expected to land on the adapter's loopback, got {landed}: "
      f"{body[:200]}", file=sys.stderr)
sys.exit(1)
