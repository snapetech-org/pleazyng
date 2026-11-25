# LG webOS lifetime unlock: Paymentwall server runbook

This integration is disabled by default. It exposes a paid lifetime entitlement
only after Paymentwall's server-side Status API confirms the exact stored
product, USD 2.99 price, approved risk state, and unrefunded status. A browser
callback or Paymentwall widget success callback never grants ownership.

As of 2026-11-16, the configured merchant project is still `SETUPNOTFINISHED`
at 47% overview completion. Its stored USD 2.99 product exists, but integration
checklist items including widget link, test login, and test pingback/payment
remain incomplete. This is not live payment approval or test qualification.

## Merchant and product setup

Use Paymentwall Digital Goods with the Smart TV SDK. Configure the stored,
one-time product exactly as:

- SKU: `pleazyng_lifetime_unlock`
- Price: `2.99 USD`
- Type: fixed, one-time purchase

The Smart TV SDK documentation says Evaluation Mode must be enabled in project
settings. Configure the Paymentwall project Pingback URL as
`https://billing.pleazyng.app/paymentwall/lg/pingback`. Request activation of
the Payment Status API and confirm that the project is LIVE before enabling
live checkout. Keep the project key and secret key in the server's secret store;
never put the secret key or grant signing key in Git, client configuration,
URLs, or logs.

Official provider references:

- [Paymentwall Smart TV SDK](https://docs.paymentwall.com/integration/sdks/smart_tv)
- [Paymentwall Digital Goods widget](https://docs.paymentwall.com/integration/widget/digital-goods)
- [Paymentwall Pingbacks](https://docs.paymentwall.com/reference/pingback-home)
- [Paymentwall Signature Calculation](https://docs.paymentwall.com/reference/signature-calculation)
- [Payment Status API, English](https://www.paymentwall.com/en/documentation/Payment-Status-API/3019), [official alternate locale](https://www.paymentwall.com/cn/documentation/Payment-Status-API/3019)
- [LG webOS IAP guidance](https://webostv.developer.lge.com/develop/guides/in-app-purchase)

The official Status API reference documents a signed GET request to
`https://api.paymentwall.com/api/rest/payment/`, using either a payment
reference or `uid` plus `ag_external_id`. Its JSON response is an array of
payment objects with `object`, `id`, `amount`, `currency`, `refunded`, `risk`,
`uid`, and `product_id`; no payments are represented by an empty array. The
production handler rejects malformed responses, duplicate reference IDs,
redirects, and non-JSON bodies. The legacy Status API can label a valid JSON
array as `text/html`. The server parses it as JSON and applies the full
payment schema checks. HTML pages and upstream error objects are rejected.
The schema fixture in
`server/lg_paymentwall_test.go` is an adapted USD 2.99 example, not a live
transaction or qualification claim.

The checkout page loads Paymentwall Smart TV SDK 1.4 from its documented
`api.paymentwall.com` URL. A 2026-11-16 inspection of that official script
recorded SHA-256 `8051e8c67fcafb3d496cd937b4398822867a8be4e73b068e690f7ae84d6056f6`:
the SDK evaluates provider responses with `eval` and builds the payment UI with
inline style attributes. The dedicated checkout response therefore allows
`'unsafe-eval'` for scripts and `style-src-attr 'unsafe-inline'` only on that
dynamic billing page, while retaining its per-response script/style nonce. The
SDK's optional Google Analytics loader stays blocked by `script-src` and
`connect-src`. The static website CSP is a separate deployment and is not
changed by these checkout requirements. Register
`PWSmartGatewayOnLoadedCallback` before loading the SDK; the gateway object is
created before its asynchronously loaded platform adapter is ready.

Paymentwall documents pingback `type=0` as a purchase, `type=2` as refund,
chargeback, or fraud, Brick `200` as pending, `201` as accepted, `202` as
declined, `203` as voided, and `220` as a successful partial refund. This
indivisible unlock is revoked for types 2, 202, 203, and 220. Pending, courtesy,
subscription, unknown, and test events never grant access. Each accepted
pingback is acknowledged with the documented `OK` body and HTTP 200.

Signature v3 is SHA-256 over alphabetically sorted `name=value` fields followed
by the project secret. This is the provider's documented concatenation format,
not HMAC. Signature v3 is required by default. Permit v2 only by setting
`LG_PAYMENTWALL_ALLOW_SIG_V2=true` after confirming the project sends v2; v1 is
always rejected.

## Safe technical-review mode before LIVE activation

Paymentwall's Status API requires provider activation for a LIVE project, while
the merchant may need to exercise a test pingback during technical review. A
separate test-only listener mode supports that order without enabling live
checkout:

1. Route `billing.pleazyng.app` over HTTPS to this Go server. Keep the static
   website and its CSP/WAF configuration separate.
2. Configure the Paymentwall project key and secret in the server secret store.
3. Set `LG_PAYMENTWALL_ENABLED=false`,
   `LG_PAYMENTWALL_TEST_PINGBACK_ENABLED=true`, and leave
   `LG_PAYMENTWALL_STATUS_API_ENABLED=false`.
4. Use Paymentwall's Test Pingback Tool or Test Payment method. The listener
   verifies the configured signature version and Paymentwall source IP, then
   acknowledges only a signed request with the documented `is_test=1` marker.
5. Confirm identity, checkout, entitlement, and unmarked live pingbacks remain
   unavailable. Test callbacks do not query Status API, create state, or issue
   an entitlement.

Run the isolated container with
`docker compose -f server/docker-compose.billing.yml --profile billing up -d`.
The normal `server/docker-compose.yml` does not receive Paymentwall secrets or
mount billing routes. The billing compose service binds only to
`127.0.0.1:8081` and the Go `-billing-only` mode serves only `/health` and the
Paymentwall paths listed in the endpoint inventory.

The billing virtual host must also reject every path outside this allowlist.
For an Nginx ingress, use exact locations like these and keep the host on its
own TLS virtual server:

```nginx
server {
    listen 443 ssl;
    server_name billing.pleazyng.app;

    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    location = /health {
        proxy_pass http://127.0.0.1:8081/health;
    }
    location = /lg/paymentwall/identity {
        proxy_pass http://127.0.0.1:8081;
    }
    location = /lg/paymentwall/checkout {
        proxy_pass http://127.0.0.1:8081;
    }
    location ~ ^/lg/paymentwall/checkout/[0-9a-f]{64}$ {
        proxy_pass http://127.0.0.1:8081;
    }
    location = /lg/paymentwall/entitlement {
        proxy_pass http://127.0.0.1:8081;
    }
    location = /paymentwall/lg/pingback {
        proxy_pass http://127.0.0.1:8081;
    }
    location / {
        return 404;
    }
}
```

Set `BILLING_TRUSTED_PROXY_CIDRS` to the proxy network that actually connects
to the container. It is independent from the user-hosted relay's proxy trust
setting. Keep the binding on loopback, do not proxy this host to the general
relay, and do not add OAuth, log, poster, commerce, or WebSocket locations.

The persistent state lock currently uses Linux `flock`; non-Linux Go builds
keep the ordinary relay available, while Paymentwall state-store startup
returns a clear billing-unavailable error. Deploy the dedicated billing
container on Linux and keep it single-writer.

The test-only mode does not qualify live payments. An unmarked callback in this
mode receives HTTP 503. When the project is LIVE and Status API activation is
confirmed, configure the grant signing key and persistent state volume, then
set `LG_PAYMENTWALL_STATUS_API_ENABLED=true` and
`LG_PAYMENTWALL_ENABLED=true`. Do not enable live checkout based only on a test
pingback.

## Runtime settings

`server/docker-compose.billing.yml` reads these variables from the billing
container's runtime environment. `server/lg-paymentwall.env.example` contains
only disabled placeholders.

| Variable | Purpose |
| --- | --- |
| `LG_PAYMENTWALL_ENABLED` | Enables identity, checkout, entitlement, and live pingbacks. Defaults to `false`. |
| `LG_PAYMENTWALL_TEST_PINGBACK_ENABLED` | Enables the signature-checked, no-state test callback listener while live mode is off. Defaults to `false`. |
| `LG_PAYMENTWALL_STATUS_API_ENABLED` | Operator assertion that Paymentwall activated Status API for the LIVE project. Required for live mode. |
| `LG_PAYMENTWALL_PROJECT_KEY` | Public Paymentwall project key. Required in test-only or live mode. |
| `LG_PAYMENTWALL_SECRET_KEY` | Paymentwall pingback and Status API signing secret. Server-only. |
| `LG_PAYMENTWALL_PUBLIC_BASE_URL` | Must be exactly `https://billing.pleazyng.app` in live mode. |
| `LG_PAYMENTWALL_STATE_FILE` | Bounded ownership state path; defaults to `/data/lg-paymentwall.json`. |
| `LG_PAYMENTWALL_GRANT_PRIVATE_KEY` | Base64 Ed25519 seed (32 bytes) or private key (64 bytes), separate from every other signing purpose. Server-only. |
| `LG_PAYMENTWALL_ALLOW_SIG_V2` | Explicit opt-in for provider signature v2. Defaults to `false`. |
| `LG_PAYMENTWALL_ALLOWED_ORIGINS` | Exact HTTPS origins for the dedicated billing API; must include `https://billing.pleazyng.app`. |

The mobile/webOS client embeds only the corresponding Ed25519 public key. Each
grant signs the UID, fixed SKU, USD 2.99, ownership state, revision, and issue
time. Persist the accepted signed grant in `CredentialKeyStore`; offline use
may preserve a previously verified lifetime grant. Lower ownership only after
a successfully verified server-signed revocation grant.

## Identity, checkout, restore, and refund behavior

The client creates a cryptographically random 128-bit UID and an independent
256-bit recovery credential, saves both in `CredentialKeyStore`, and presents
the credential once as a user-controlled recovery code. The backend stores only
the SHA-256 digest of that credential. The same UID and recovery credential can
be registered from another TV after key-store loss; no hardware ID, Plex
account, or new mandatory account is used. Never log or put the credential or
recovery code in a URL. The API sends it only in an `Authorization: Bearer`
header.

1. `POST /lg/paymentwall/identity` registers or re-authenticates the UID.
2. `POST /lg/paymentwall/checkout` creates a random, ten-minute checkout-session
   capability and returns a URL on the billing origin. It contains no UID
   credential or recovery code.
3. The checkout page uses Paymentwall's Smart TV SDK. Its completion callback
   only tells the user to return to the app and restore.
4. `GET /lg/paymentwall/entitlement` queries Status API by UID and SKU, checks
   every payment object, durably reconciles references, then returns a signed
   grant. A failed query or failed persistence cannot create an owned grant.
5. `/paymentwall/lg/pingback` checks the provider source subnet and signature.
   Positive types 0 and 201 must also match a fresh Status API response. Negative
   pingbacks revoke the referenced transaction immediately. A separate valid
   transaction can keep the indivisible lifetime unlock active.

Recovery import sends `{"uid":"…","restoreOnly":true}` to the identity
route. The server returns 204 only when that UID already exists and the bearer
credential matches its stored hash; an unknown UID returns a generic 401 and
is never registered by restore. The client stages the candidate credential
and signed ownership proof before switching its active identity, and retains
the previous identity if verification or persistence fails.

Each reference has a persisted mutation generation. The service captures
all generations for the UID before a Status API request and reconciles the
response only against references that have not changed since that snapshot.
This fence prevents an older response from overwriting a positive pingback,
refund, or newer reconciliation even if wall-clock timestamps tie or move
backwards; timestamps remain available for diagnostics. Every verified negative
pingback durably advances its reference generation, including a repeated refund
for an already-revoked reference. A fresh query started after the latest refund
may reinstate the same reference only if Paymentwall currently reports the
exact SKU, USD 2.99, approved risk, and `refunded=false`.

## State file, single writer, and backup

The JSON state file contains credential hashes, UID ownership and revision,
verified payment references, event deduplication records, and short-lived
checkout-session digests. It is capped at 8 MiB and bounded by record limits.
The store holds an OS-level exclusive lock for the process lifetime and uses an
in-process mutex. Writes use a mode-0600 temporary file, file `fsync`, atomic
rename, and directory `fsync`; memory changes only after the durable write
completes. A corrupt, oversized, symlinked, permissive, or concurrently opened
state file fails startup closed.

If directory `fsync` or close fails after rename, the running store is marked
poisoned: all billing routes fail closed until restart and recovery. Do not
manually reset the state. On restart, the persisted snapshot is loaded and a
fresh Status API query must complete and durably reconcile before a signed
ownership grant is returned.

Run exactly one billing container with this state volume. Do not scale the
Paymentwall-owning service horizontally or copy a live state file while the
container is writing. For a consistent backup, stop the billing container, copy
`/data/lg-paymentwall.json` to encrypted restricted storage, then restart the
service. Do not back up the `.lock` file. Keep the Ed25519 private key in the
secret manager separately; it is not part of the state backup. To restore,
stop the billing service, restore the file with owner and mode `0600`, verify
that only one billing instance will start, then start the service. Preserve an encrypted backup
before any manual recovery; never reset the state to make startup pass.

The API is intentionally disabled until live keys, Status API activation,
HTTPS routing, and single-writer persistent storage are configured. There is no
LG-store free fallback.
