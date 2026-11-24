#!/usr/bin/env python3
"""Minimal headless-Chromium driver over the DevTools pipe (standard library only).

Used by fetch.py's session transport. It keeps a private persistent
profile so the Reddit sign-in survives between runs, signs in with
REDDIT_USERNAME / REDDIT_PASSWORD from the environment only when the session is
gone, and fetches same-origin reddit.com JSON through the signed-in page.

Commands (all print one JSON object per line on stdout):
  status            is the profile signed in? {"signed_in": bool, "user": name}
  login             sign in if needed (reads REDDIT_USERNAME/REDDIT_PASSWORD)
  get URL...        fetch each reddit.com path/URL through the session;
                    prints {"url", "status", "body"} per URL
  serve             read one URL per stdin line, answer like ``get``
                    (lets fetch.py pace requests itself)

Secrets are never printed, logged, written or put in argv.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

CHROME_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome")
PLAYWRIGHT_GLOB = "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"
ORIGIN = "https://www.reddit.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


class BrowserError(RuntimeError):
    pass


def find_chrome(explicit=None):
    """An explicit path, then REDDIT_CHROME, then Chromium on PATH, then a
    Playwright-installed Chromium."""
    for candidate in (explicit, os.environ.get("REDDIT_CHROME")):
        if candidate:
            return candidate
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    installed = sorted(glob.glob(os.path.expanduser(PLAYWRIGHT_GLOB)))
    return installed[-1] if installed else None


class Chrome:
    """One Chromium process driven through --remote-debugging-pipe."""

    def __init__(self, profile, chrome=None, headless=True):
        binary = find_chrome(chrome)
        if not binary:
            raise BrowserError("no Chromium binary found (set REDDIT_CHROME)")
        os.makedirs(profile, mode=0o700, exist_ok=True)
        for stale in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                os.unlink(os.path.join(profile, stale))
            except FileNotFoundError:
                pass
        to_chrome_r, self._w = os.pipe()
        self._r, from_chrome_w = os.pipe()
        args = [
            binary,
            "--remote-debugging-pipe",
            f"--user-data-dir={profile}",
            "--no-sandbox",  # containers often have no user namespaces
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--mute-audio",
            "--window-size=1280,900",
            f"--user-agent={USER_AGENT}",
            "--lang=en-US",
            "about:blank",
        ]
        if headless:
            args.insert(1, "--headless=new")
        self._devnull = open(os.devnull, "wb")

        def plumbing():
            # Move both ends above 4 first so neither dup2 clobbers the other.
            a = os.dup(to_chrome_r) if to_chrome_r < 5 else to_chrome_r
            b = os.dup(from_chrome_w) if from_chrome_w < 5 else from_chrome_w
            a = os.dup(a) if a < 5 else a
            b = os.dup(b) if b < 5 else b
            os.dup2(a, 3)
            os.dup2(b, 4)

        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=self._devnull,
            stderr=self._devnull,
            preexec_fn=plumbing,
            close_fds=False,
        )
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._buf = b""
        self._next = 0
        self._events = []
        self.session = None

    # -- protocol ---------------------------------------------------------
    def _send(self, method, params=None, session=None):
        self._next += 1
        message = {"id": self._next, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        os.write(self._w, json.dumps(message).encode() + b"\0")
        return self._next

    def _read(self, timeout):
        import select

        deadline = time.monotonic() + timeout
        while b"\0" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise BrowserError("timed out waiting for Chromium")
            ready, _, _ = select.select([self._r], [], [], left)
            if ready:
                chunk = os.read(self._r, 1 << 20)
                if not chunk:
                    raise BrowserError("Chromium exited")
                self._buf += chunk
        raw, self._buf = self._buf.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method, params=None, session="page", timeout=60):
        target = self.session if session == "page" else session
        wanted = self._send(method, params, target)
        while True:
            message = self._read(timeout)
            if message.get("id") == wanted:
                if "error" in message:
                    raise BrowserError(f"{method}: {message['error'].get('message')}")
                return message.get("result", {})
            if "method" in message:
                self._events.append(message)

    def wait_event(self, method, timeout=60):
        for i, event in enumerate(self._events):
            if event["method"] == method:
                return self._events.pop(i)
        deadline = time.monotonic() + timeout
        while True:
            message = self._read(max(0.1, deadline - time.monotonic()))
            if message.get("method") == method:
                return message
            if "method" in message:
                self._events.append(message)

    # -- page helpers -----------------------------------------------------
    def open_page(self):
        target = self.call("Target.createTarget", {"url": "about:blank"}, session=None)
        attached = self.call(
            "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True},
            session=None,
        )
        self.session = attached["sessionId"]
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
        )

    def goto(self, url, timeout=60):
        self._events = [e for e in self._events if e["method"] != "Page.loadEventFired"]
        self.call("Page.navigate", {"url": url})
        try:
            self.wait_event("Page.loadEventFired", timeout)
        except BrowserError:
            pass

    def evaluate(self, expression, timeout=60):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            text = result["exceptionDetails"].get("exception", {}).get("description")
            raise BrowserError(f"page script failed: {text or result['exceptionDetails'].get('text')}")
        return result.get("result", {}).get("value")

    def close(self):
        try:
            self.call("Browser.close", session=None, timeout=10)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass
        self._devnull.close()


FETCH_JS = """
(async (url) => {
  const r = await fetch(url, {credentials: 'include', headers: {'Accept': 'application/json'}});
  return {status: r.status, body: await r.text()};
})(%s)
"""

ME_JS = """
(async () => {
  try {
    const r = await fetch('/api/me.json', {credentials: 'include'});
    if (!r.ok) return {status: r.status};
    const j = await r.json();
    return {status: r.status, name: (j.data || j).name || null};
  } catch (e) { return {status: 0, error: String(e)}; }
})()
"""


def whoami(chrome):
    value = chrome.evaluate(ME_JS) or {}
    return value.get("name")


def ensure_page(chrome):
    if chrome.session is None:
        chrome.open_page()
        chrome.goto(ORIGIN + "/")


def login(chrome, username, password, timeout=90):
    """Sign in through the real login page. Returns the signed-in name."""
    ensure_page(chrome)
    if whoami(chrome):
        return whoami(chrome)
    chrome.goto(ORIGIN + "/login/")
    time.sleep(3)
    # The login form lives in shadow DOM web components; find inputs deeply.
    fill = """
    ((user, pass) => {
      const all = [];
      const walk = (root) => { root.querySelectorAll('*').forEach(el => { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); }); };
      walk(document);
      const u = all.find(e => e.tagName === 'INPUT' && (e.name === 'username' || e.id === 'login-username' || e.autocomplete === 'username'));
      const p = all.find(e => e.tagName === 'INPUT' && (e.name === 'password' || e.type === 'password'));
      if (!u || !p) return {ok: false, why: 'login form not found'};
      return {ok: true};
    })(%s, %s)
    """
    found = chrome.evaluate(fill % (json.dumps(""), json.dumps("")))
    if not found or not found.get("ok"):
        raise BrowserError((found or {}).get("why") or "login form not found")
    for field, value in (("username", username), ("password", password)):
        focus = """
        ((which) => {
          const all = [];
          const walk = (root) => { root.querySelectorAll('*').forEach(el => { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); }); };
          walk(document);
          const el = which === 'username'
            ? all.find(e => e.tagName === 'INPUT' && (e.name === 'username' || e.id === 'login-username' || e.autocomplete === 'username'))
            : all.find(e => e.tagName === 'INPUT' && (e.name === 'password' || e.type === 'password'));
          el.focus(); el.select && el.select(); return true;
        })(%s)
        """ % json.dumps(field)
        chrome.evaluate(focus)
        # Real key input so the page's own handlers see typing.
        chrome.call("Input.insertText", {"text": value})
        time.sleep(0.4)
    chrome.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
    chrome.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            name = whoami(chrome)
        except BrowserError:
            name = None
        if name:
            return name
    # Describe what the page shows (no secrets: only visible headings/errors).
    hint = chrome.evaluate(
        "(() => { const t=[]; const walk=(r)=>{r.querySelectorAll('*').forEach(e=>{ if(/^(H1|H2|P|SPAN|FAUXINPUT)$/.test(e.tagName)||e.getAttribute&&e.getAttribute('role')==='alert'){const s=(e.textContent||'').trim(); if(s&&s.length<160) t.push(s);} if(e.shadowRoot) walk(e.shadowRoot);});}; walk(document); return [location.pathname, [...new Set(t)].slice(0,25)]; })()"
    )
    raise BrowserError("sign-in did not complete: " + json.dumps(hint)[:1500])


def get(chrome, url):
    ensure_page(chrome)
    if url.startswith("/"):
        url = ORIGIN + url
    if not url.startswith(ORIGIN + "/"):
        raise BrowserError("only www.reddit.com URLs are allowed")
    value = chrome.evaluate(FETCH_JS % json.dumps(url), timeout=90) or {}
    return {"url": url, "status": value.get("status"), "body": value.get("body")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, help="private persistent profile directory")
    parser.add_argument("--chrome", help="Chromium binary (default: REDDIT_CHROME, then PATH)")
    parser.add_argument("command", choices=["status", "login", "get", "serve"])
    parser.add_argument("urls", nargs="*")
    args = parser.parse_args(argv)
    chrome = Chrome(args.profile, args.chrome)
    out = sys.stdout
    try:
        ensure_page(chrome)
        if args.command == "status":
            name = whoami(chrome)
            out.write(json.dumps({"signed_in": bool(name), "user": name}) + "\n")
        elif args.command == "login":
            username = os.environ.get("REDDIT_USERNAME") or ""
            password = os.environ.get("REDDIT_PASSWORD") or ""
            if not username or not password:
                raise BrowserError("REDDIT_USERNAME and REDDIT_PASSWORD are required")
            name = login(chrome, username, password)
            out.write(json.dumps({"signed_in": True, "user": name}) + "\n")
        elif args.command == "get":
            for url in args.urls:
                out.write(json.dumps(get(chrome, url)) + "\n")
                out.flush()
        else:
            for line in sys.stdin:
                url = line.strip()
                if url:
                    try:
                        out.write(json.dumps(get(chrome, url)) + "\n")
                    except BrowserError as error:
                        out.write(json.dumps({"url": url, "status": 0, "error": str(error)}) + "\n")
                    out.flush()
    except BrowserError as error:
        out.write(json.dumps({"error": str(error)}) + "\n")
        return 1
    finally:
        chrome.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
