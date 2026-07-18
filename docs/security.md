# Security notes

Written for the case this project invites: running the dashboard somewhere
other people can reach it.

## Threat model

The asset worth protecting is the **ecobee OAuth token**. It can change your
thermostats. The readings themselves are lower-stakes but not nothing: a
temperature/occupancy history reveals when a building is empty.

## What the design already does

**Credentials are never on the public surface.** The browser talks only to the
dashboard, which is a GET-only viewer of a local SQLite file. Ecobee
credentials are used for *outbound* calls by the logger. There is no request a
visitor can send that reaches the ecobee API — read or write.

**Token isolation between containers.** `data/auth/` is mounted only into the
logger. The web-facing container has no filesystem path to the token file,
so compromising it does not yield ecobee credentials. Verify on your own
install:

```bash
docker compose exec dashboard ls /data        # shows: db
docker compose exec dashboard cat /data/auth/ecobee.conf   # No such file
```

**No password at rest.** `ecobee_login.py` scrubs the password before writing;
only tokens are stored, mode 600. The refresh-token grant means the password
is never needed again.

**Identifiers are masked in the API.** Thermostat identifiers are serial
numbers. The JSON API replaces them with salted SHA-256 hashes (salt is random
per install, stored in `meta.public_id_salt`), so serials don't leak to
browsers. The hashes are stable, so client-side preferences still work.

**Errors don't leak internals.** API errors return a generic message; details
go to the server log.

**Repeat requests are cheap.** A 60-second response cache bounds the cost of
hammering `/api/data`.

**Secrets can't be committed.** `.gitignore` blocks `data/`, `*.sqlite3`,
`*.conf` and `.env`.

## If you expose it to the internet

The above makes exposure *safe for your credentials*. It does not make your
occupancy data private. Decide deliberately:

1. **Put authentication in front of it.** The app has no user accounts by
   design; use your reverse proxy or edge. Caddy:

   ```caddy
   basic_auth /ecobee/* {
       yourname <bcrypt-hash>      # docker exec -it caddy caddy hash-password
   }
   ```

   Or an identity-aware proxy (Cloudflare Access, Tailscale, oauth2-proxy).

2. **Rate-limit the API path.** The response cache protects your server; a
   rate limit protects your bandwidth and logs. Any edge or proxy can do this;
   e.g. >10 requests / 10 seconds per IP → block.

3. **Prefer a private network first.** A VPN or overlay network (WireGuard,
   Tailscale) gives you remote access with no public surface at all. Reach for
   public exposure only when you actually need to show someone.

4. **Keep the host patched.** The write-capable token lives on that machine —
   protecting the host is what protects the thermostats.

## Revoking access

If a token is ever exposed: change your ecobee.com password, which invalidates
issued tokens, then re-run `./setup.sh` (or `ecobee_login.py`) to obtain new
ones. Deleting `data/auth/ecobee.conf` only removes your local copy — it does
not revoke anything on ecobee's side.

## Reporting a problem

Please open an issue for anything non-sensitive. For a suspected
vulnerability, contact the maintainer privately via
[sweetbriarcomputing.com](https://sweetbriarcomputing.com) rather than filing
a public issue.
