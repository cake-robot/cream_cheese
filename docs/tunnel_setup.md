# Cloudflare Tunnel setup

Makes the web UI reachable from your phone (or anyone with an invite code)
without opening a port on your router or exposing your home IP. `cloudflared`
runs on this Mac and makes an *outbound* connection to Cloudflare; Cloudflare
proxies public HTTPS traffic through that connection to `127.0.0.1:5050`,
where `serve.py` is already listening. The app's own login wall (see
`serve.py`) is the only auth layer needed -- there's no reason to also put
this behind Cloudflare Access, since every route already requires a session.

None of this is done for you -- run these yourself, in order, on this
machine. Nothing here is reversible-by-Claude (it needs your browser login
and, for the last step, `sudo`), which is why it's a doc instead of a script.

## 0. Prerequisites

- **A domain already registered as a zone in your Cloudflare account --
  before step 2.** A Cloudflare account by itself has no zones; without one,
  `cloudflared tunnel login`'s zone picker is just an empty table with a
  "Connect your website or app" link and nothing to select. Two ways to get
  one:
  - Buy one via Cloudflare Registrar (~$10.44/yr for a `.com`, at cost, no
    renewal markup): dash.cloudflare.com → **Domain Registration** →
    **Register a Domain**. Lands as an active zone immediately -- no
    nameserver propagation wait, since it's Cloudflare-hosted DNS from the
    start.
  - Already own a domain elsewhere: dash.cloudflare.com → **Add a site**,
    then update your registrar's nameservers to the two Cloudflare gives
    you. Slower -- propagation can take a few hours.
- `brew` installed (it already is, since the repo's `venv` assumes it).

## 1. Install cloudflared

```
brew install cloudflared
```

## 2. Authenticate

```
cloudflared tunnel login
```

Opens a browser tab, asks you to pick the Cloudflare zone (your domain) to
authorize. Creates `~/.cloudflared/cert.pem`.

## 3. Create the tunnel

```
cloudflared tunnel create cream-cheese
```

Prints a tunnel UUID and writes credentials to
`~/.cloudflared/<UUID>.json`. Note the UUID for the next step.

## 4. Configure the route

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: cream-cheese
credentials-file: /Users/ericsayre/.cloudflared/<UUID>.json

ingress:
  - hostname: cfb.yourdomain.com
    service: http://127.0.0.1:5050
  - service: http_status:404
```

Replace `<UUID>` and `cfb.yourdomain.com` with your actual values. The
trailing `http_status:404` catch-all is required by cloudflared -- every
`ingress` list needs a final rule with no `hostname`.

Then point DNS at it:

```
cloudflared tunnel route dns cream-cheese cfb.yourdomain.com
```

## 5. Run cloudflared as a service

```
sudo cloudflared service install
```

This is the one step that needs `sudo` -- it installs cloudflared as a
system-level launchd daemon (separate from this repo's own
`com.creamcheese.*` agents in `deploy/`), so it starts on boot and survives
logout, same as you'd want for `serve`/`live`.

Verify it's running:

```
sudo launchctl print system/com.cloudflare.cloudflared
```

## 6. Tell serve.py about the public origin

`serve.py` needs `CC_PUBLIC_ORIGIN` set to add the tunnel hostname to its
CSRF origin allowlist and turn on `Secure` session cookies (see
`serve.py`'s `ALLOWED_POST_ORIGINS` / `SESSION_COOKIE_SECURE`). Edit
`deploy/com.creamcheese.serve.plist`, uncomment the `EnvironmentVariables`
block near the top, and fill in your real hostname:

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>CC_PUBLIC_ORIGIN</key>
    <string>https://cfb.yourdomain.com</string>
</dict>
```

If you haven't run `just install-services` yet, do that now -- it copies
this plist into `~/Library/LaunchAgents` and bootstraps it. If you already
had, reinstall to pick up the edit:

```
just install-services
```

(`install-services` bootouts before bootstrapping, so this is safe to
re-run any time you edit a plist.)

## 7. Verify end-to-end

- `curl -sI https://cfb.yourdomain.com/api/healthz` from another network
  (phone on cellular, not your home WiFi) -- should get a real HTTP
  response through Cloudflare's edge, not a timeout. Since `/api/healthz`
  requires auth once `CF-Connecting-IP` is present (see `serve.py`'s
  `_require_auth`), a 401 here is actually the *correct* signed-out
  response -- it proves the request reached serve.py and the auth gate
  correctly told the tunnel apart from a loopback `curl`.
- Visit `https://cfb.yourdomain.com/login.html` on your phone, log in, and
  confirm you land on the settings page.
- Change a spoiler setting from the phone -- this is the one thing most
  likely to 403 if the origin allowlist isn't picking up `CC_PUBLIC_ORIGIN`
  correctly (see `serve.py`'s `_guard_writes`). If it does 403, double
  check step 6 actually took effect: `launchctl print
  gui/$(id -u)/com.creamcheese.serve | grep CC_PUBLIC_ORIGIN`.

## Rolling back

- Stop just the tunnel: `sudo launchctl bootout system/com.cloudflare.cloudflared`
- Fully remove it: `sudo cloudflared service uninstall`
- The app itself (`serve`/`live`) is unaffected either way -- it keeps
  running on loopback, just not reachable from outside until the tunnel is
  back.
