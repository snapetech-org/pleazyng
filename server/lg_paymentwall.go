package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/md5"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"html/template"
	"io"
	"mime"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	lgPaymentwallIdentityPath       = "/lg/paymentwall/identity"
	lgPaymentwallCheckoutPath       = "/lg/paymentwall/checkout"
	lgPaymentwallCheckoutPage       = lgPaymentwallCheckoutPath + "/"
	lgPaymentwallEntitlementPath    = "/lg/paymentwall/entitlement"
	lgPaymentwallPingbackPath       = "/paymentwall/lg/pingback"
	lgPaymentwallProductSKU         = "pleazyng_lifetime_unlock"
	lgPaymentwallCurrency           = "USD"
	lgPaymentwallAmountCents        = 299
	lgPaymentwallStateVersion       = 1
	lgPaymentwallGrantVersion       = 1
	lgPaymentwallDefaultStateFile   = "/data/lg-paymentwall.json"
	lgPaymentwallDefaultStatusURL   = "https://api.paymentwall.com/api/rest/payment/"
	lgPaymentwallMaxBodyBytes       = 16 * 1024
	lgPaymentwallMaxStateBytes      = 8 * 1024 * 1024
	lgPaymentwallMaxIdentities      = 100_000
	lgPaymentwallMaxReferences      = 200_000
	lgPaymentwallMaxEvents          = 400_000
	lgPaymentwallMaxCheckoutPages   = 10_000
	lgPaymentwallCheckoutSessionTTL = 10 * time.Minute
	lgPaymentwallRateBurst          = 30
	lgPaymentwallRateWindow         = time.Minute
)

var (
	lgPaymentwallUIDPattern              = regexp.MustCompile(`^[a-f0-9]{32}$`)
	lgPaymentwallCredentialPattern       = regexp.MustCompile(`^[a-f0-9]{64}$`)
	lgPaymentwallReferencePattern        = regexp.MustCompile(`^[A-Za-z0-9]{1,128}$`)
	lgPaymentwallProjectKeyPattern       = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)
	lgPaymentwallTrustedPingbackNetworks = []netip.Prefix{netip.MustParsePrefix("216.127.71.0/24")}
)

var errLGPaymentwallDisabled = errors.New("Paymentwall is not configured")
var errLGPaymentwallInvalidState = errors.New("Paymentwall state is invalid")
var errLGPaymentwallStateFull = errors.New("Paymentwall state capacity reached")
var errLGPaymentwallUnauthorized = errors.New("Paymentwall identity is not authorized")
var errLGPaymentwallStatusUnavailable = errors.New("Paymentwall status is not authoritative")
var errLGPaymentwallStorePoisoned = errors.New("Paymentwall state store requires restart and recovery")

type lgPaymentwallConfig struct {
	enabled             bool
	testPingbackEnabled bool
	publicBaseURL       *url.URL
	allowedOrigins      map[string]struct{}
	projectKey          string
	secretKey           string
	stateFile           string
	grantPrivateKey     ed25519.PrivateKey
	allowSignatureV2    bool
	statusAPIURL        string
}

type lgPaymentwallIdentity struct {
	CredentialHash string    `json:"credentialHash"`
	Owned          bool      `json:"owned"`
	Revision       uint64    `json:"revision"`
	CreatedAt      time.Time `json:"createdAt"`
}

type lgPaymentwallReference struct {
	UID       string    `json:"uid"`
	SKU       string    `json:"sku"`
	Verified  bool      `json:"verified"`
	Revoked   bool      `json:"revoked"`
	Mutation  uint64    `json:"mutation"`
	UpdatedAt time.Time `json:"updatedAt"`
}

// lgPaymentwallMutationFence records each reference generation immediately
// before an authoritative Status API request. It prevents an older response
// from overwriting a pingback or a newer reconciliation even when clocks tie
// or move backwards.
type lgPaymentwallMutationFence map[string]uint64

type lgPaymentwallEvent struct {
	UID        string    `json:"uid"`
	Ref        string    `json:"ref"`
	Type       int       `json:"type"`
	ReceivedAt time.Time `json:"receivedAt"`
}

type lgPaymentwallCheckoutSession struct {
	UID       string    `json:"uid"`
	ExpiresAt time.Time `json:"expiresAt"`
}

type lgPaymentwallState struct {
	Version    int                                     `json:"version"`
	Identities map[string]lgPaymentwallIdentity        `json:"identities"`
	References map[string]lgPaymentwallReference       `json:"references"`
	Events     map[string]lgPaymentwallEvent           `json:"events"`
	Sessions   map[string]lgPaymentwallCheckoutSession `json:"sessions"`
}

type lgPaymentwallStore struct {
	path     string
	lockFile *os.File
	syncDir  func(string) error
	mu       sync.Mutex
	state    lgPaymentwallState
	poisoned bool
}

type lgPaymentwallOwnershipSnapshot struct {
	Owned    bool
	Revision uint64
}

type lgPaymentwallRateGate struct {
	mu      sync.Mutex
	window  time.Duration
	limit   int
	maxKeys int
	hits    map[string][]time.Time
}

type lgPaymentwallStatusAPI struct {
	projectKey string
	secretKey  string
	endpoint   string
	client     *http.Client
}

type lgPaymentwallPayment struct {
	Object    string `json:"object"`
	ID        string `json:"id"`
	UID       string `json:"uid"`
	ProductID string `json:"product_id"`
	Amount    string `json:"amount"`
	Currency  string `json:"currency"`
	Refunded  *bool  `json:"refunded"`
	Risk      string `json:"risk"`
}

type lgPaymentwallGrantPayload struct {
	Version     int       `json:"version"`
	UID         string    `json:"uid"`
	ProductID   string    `json:"productId"`
	Currency    string    `json:"currency"`
	AmountCents int       `json:"amountCents"`
	Owned       bool      `json:"owned"`
	Revision    uint64    `json:"revision"`
	IssuedAt    time.Time `json:"issuedAt"`
}

type lgPaymentwallEntitlementResponse struct {
	Grant string `json:"grant"`
}

type lgPaymentwallIdentityRequest struct {
	UID         string `json:"uid"`
	RestoreOnly bool   `json:"restoreOnly,omitempty"`
}

type lgPaymentwallCheckoutResponse struct {
	URL string `json:"url"`
}

type lgPaymentwallHandler struct {
	config     lgPaymentwallConfig
	clientIPs  clientIPResolver
	store      *lgPaymentwallStore
	statusAPI  *lgPaymentwallStatusAPI
	client     *http.Client
	now        func() time.Time
	ipRate     *lgPaymentwallRateGate
	uidRate    *lgPaymentwallRateGate
	globalRate *lgPaymentwallRateGate
}

type lgPaymentwallCheckoutTemplateData struct {
	Nonce      string
	ProjectKey string
	UID        string
	ProductSKU string
}

var lgPaymentwallCheckoutHTML = template.Must(template.New("lg-paymentwall-checkout").Parse(`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pleazyng lifetime unlock</title>
  <style nonce="{{.Nonce}}">
    :root { color-scheme: dark; font: 24px system-ui, sans-serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #111318; color: #f3f4f6; }
    main { width: min(42rem, calc(100% - 3rem)); padding: 2rem; border: 1px solid #424752; border-radius: 1rem; background: #1b1e25; }
    h1 { font-size: 1.4rem; margin-top: 0; }
    p { line-height: 1.5; color: #d2d5dc; }
    #payment { min-height: 16rem; }
    #status { font-size: 1rem; }
  </style>
</head>
<body>
  <main>
    <h1>Pleazyng lifetime access</h1>
    <p>Complete the one-time USD 2.99 purchase to unlock Pleazyng on this TV. After payment, return to Pleazyng and choose Restore to verify your purchase.</p>
    <div id="payment" aria-live="polite">Loading the secure payment form…</div>
    <p id="status">Pleazyng unlocks after the payment provider confirms your payment.</p>
  </main>
  <script nonce="{{.Nonce}}">
    (function () {
      const status = document.getElementById('status');
      function showError() { status.textContent = 'Payment could not be completed. Return to Pleazyng and try again.'; }
      let formOpened = false;
      function openPaymentForm() {
        if (formOpened) return;
        formOpened = true;
        try {
          PWSmartGateway.init({ key: {{printf "%q" .ProjectKey}}, lang: 'en', brand: 'lgtv' });
          PWSmartGateway.showPaymentForm({
            productId: {{printf "%q" .ProductSKU}},
            userId: {{printf "%q" .UID}},
            showBackButton: true,
            enablePaymentRecoverFlow: true
          }, function () {
            status.textContent = 'Payment submitted. Return to Pleazyng and choose Restore.';
          }, showError);
        } catch (_) { showError(); }
      }
      // Paymentwall's documented callback fires only after its asynchronously
      // loaded platform adapter is ready. Register it before inserting the SDK;
      // the gateway object itself exists earlier and is not a readiness signal.
      window.PWSmartGatewayOnLoadedCallback = openPaymentForm;
      const sdk = document.createElement('script');
      sdk.src = 'https://api.paymentwall.com/js/pwsmart/ver/pwsmart.1.4.js';
      sdk.async = true;
      sdk.onerror = showError;
      document.head.appendChild(sdk);
    }());
  </script>
</body>
</html>`))

func newLGPaymentwallHandler(clientIPs clientIPResolver) (*lgPaymentwallHandler, error) {
	liveEnabled := envEnabled("LG_PAYMENTWALL_ENABLED")
	testPingbackEnabled := envEnabled("LG_PAYMENTWALL_TEST_PINGBACK_ENABLED")
	if !liveEnabled && !testPingbackEnabled {
		return &lgPaymentwallHandler{clientIPs: clientIPs, now: time.Now}, nil
	}
	config, err := lgPaymentwallConfigFromEnv()
	if err != nil {
		return nil, err
	}
	var store *lgPaymentwallStore
	if config.enabled {
		store, err = newLGPaymentwallStore(config.stateFile)
		if err != nil {
			return nil, err
		}
	}
	return newLGPaymentwallHandlerWithDependencies(config, clientIPs, store, &http.Client{
		Timeout: 8 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}, nil), nil
}

func lgPaymentwallConfigFromEnv() (lgPaymentwallConfig, error) {
	liveEnabled := envEnabled("LG_PAYMENTWALL_ENABLED")
	testPingbackEnabled := envEnabled("LG_PAYMENTWALL_TEST_PINGBACK_ENABLED")
	if !liveEnabled && !testPingbackEnabled {
		return lgPaymentwallConfig{}, errLGPaymentwallDisabled
	}
	if liveEnabled && !envEnabled("LG_PAYMENTWALL_STATUS_API_ENABLED") {
		return lgPaymentwallConfig{}, errors.New("Paymentwall Status API activation is required")
	}
	projectKey := strings.TrimSpace(os.Getenv("LG_PAYMENTWALL_PROJECT_KEY"))
	secretKey := strings.TrimSpace(os.Getenv("LG_PAYMENTWALL_SECRET_KEY"))
	if projectKey == "" || secretKey == "" {
		return lgPaymentwallConfig{}, errors.New("Paymentwall configuration is incomplete")
	}
	if !lgPaymentwallProjectKeyPattern.MatchString(projectKey) {
		return lgPaymentwallConfig{}, errors.New("invalid Paymentwall project key")
	}
	var baseURL *url.URL
	stateFile := ""
	var privateKey ed25519.PrivateKey
	if liveEnabled {
		baseURLRaw := strings.TrimSpace(os.Getenv("LG_PAYMENTWALL_PUBLIC_BASE_URL"))
		stateFile = strings.TrimSpace(os.Getenv("LG_PAYMENTWALL_STATE_FILE"))
		privateKeyRaw := strings.TrimSpace(os.Getenv("LG_PAYMENTWALL_GRANT_PRIVATE_KEY"))
		if baseURLRaw == "" || privateKeyRaw == "" {
			return lgPaymentwallConfig{}, errors.New("Paymentwall live configuration is incomplete")
		}
		var err error
		baseURL, err = url.Parse(baseURLRaw)
		if err != nil || baseURL.Scheme != "https" || baseURL.Host != "billing.pleazyng.app" || baseURL.User != nil ||
			baseURL.Path != "" || baseURL.RawQuery != "" || baseURL.Fragment != "" {
			return lgPaymentwallConfig{}, errors.New("Paymentwall public base URL must be https://billing.pleazyng.app")
		}
		if stateFile == "" {
			stateFile = lgPaymentwallDefaultStateFile
		}
		privateKey, err = decodeLGPaymentwallPrivateKey(privateKeyRaw)
		if err != nil {
			return lgPaymentwallConfig{}, errors.New("invalid Paymentwall grant signing key")
		}
	}
	origins := map[string]struct{}{"https://billing.pleazyng.app": {}}
	if rawOrigins := strings.TrimSpace(os.Getenv("LG_PAYMENTWALL_ALLOWED_ORIGINS")); rawOrigins != "" {
		parsedOrigins, err := parseLGPaymentwallOrigins(rawOrigins)
		if err != nil {
			return lgPaymentwallConfig{}, err
		}
		origins = parsedOrigins
	}
	if _, ok := origins["https://billing.pleazyng.app"]; !ok {
		return lgPaymentwallConfig{}, errors.New("Paymentwall origin allowlist must include https://billing.pleazyng.app")
	}
	return lgPaymentwallConfig{
		enabled:             liveEnabled,
		testPingbackEnabled: testPingbackEnabled,
		publicBaseURL:       baseURL,
		allowedOrigins:      origins,
		projectKey:          projectKey,
		secretKey:           secretKey,
		stateFile:           stateFile,
		grantPrivateKey:     privateKey,
		allowSignatureV2:    envEnabled("LG_PAYMENTWALL_ALLOW_SIG_V2"),
		statusAPIURL:        lgPaymentwallDefaultStatusURL,
	}, nil
}

func newLGPaymentwallHandlerWithDependencies(config lgPaymentwallConfig, clientIPs clientIPResolver, store *lgPaymentwallStore, client *http.Client, statusClient *lgPaymentwallStatusAPI) *lgPaymentwallHandler {
	if client == nil {
		client = &http.Client{Timeout: 8 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	}
	if statusClient == nil && config.enabled {
		endpoint := config.statusAPIURL
		if endpoint == "" {
			endpoint = lgPaymentwallDefaultStatusURL
		}
		statusClient = &lgPaymentwallStatusAPI{
			projectKey: config.projectKey,
			secretKey:  config.secretKey,
			endpoint:   endpoint,
			client:     client,
		}
	}
	return &lgPaymentwallHandler{
		config:     config,
		clientIPs:  clientIPs,
		store:      store,
		statusAPI:  statusClient,
		client:     client,
		now:        time.Now,
		ipRate:     newLGPaymentwallRateGate(lgPaymentwallRateBurst, lgPaymentwallRateWindow, 8192),
		uidRate:    newLGPaymentwallRateGate(lgPaymentwallRateBurst, lgPaymentwallRateWindow, 100_000),
		globalRate: newLGPaymentwallRateGate(120, time.Minute, 1),
	}
}

func registerLGPaymentwallRoutes(mux *http.ServeMux, handler *lgPaymentwallHandler) {
	if handler == nil {
		handler = &lgPaymentwallHandler{now: time.Now}
	}
	mux.HandleFunc(lgPaymentwallIdentityPath, handler.serveIdentity)
	mux.HandleFunc(lgPaymentwallCheckoutPath, handler.serveCheckoutSession)
	mux.HandleFunc(lgPaymentwallCheckoutPage, handler.serveCheckoutPage)
	mux.HandleFunc(lgPaymentwallEntitlementPath, handler.serveEntitlement)
	mux.HandleFunc(lgPaymentwallPingbackPath, handler.servePingback)
}

func (h *lgPaymentwallHandler) serveIdentity(w http.ResponseWriter, r *http.Request) {
	if h.prepareClientRequest(w, r, "POST") {
		return
	}
	if h.disabled(w) {
		return
	}
	ip, ok := h.admit(w, r, "identity", "")
	if !ok {
		return
	}
	_ = ip
	if !requireJSON(w, r) {
		return
	}
	var input lgPaymentwallIdentityRequest
	if !decodeLGPaymentwallJSON(w, r, &input) {
		return
	}
	credential, ok := h.authenticateCredential(w, r)
	if !ok {
		return
	}
	if !lgPaymentwallUIDPattern.MatchString(input.UID) {
		http.Error(w, "Invalid license identity", http.StatusBadRequest)
		return
	}
	if input.RestoreOnly {
		// Recovery code import must prove an identity already registered by the
		// original client. It must never create a new UID that could later be
		// mistaken for a restored purchase.
		if !h.store.authorized(input.UID, credential) {
			http.Error(w, "Payment identity could not be restored", http.StatusUnauthorized)
			return
		}
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if err := h.store.registerIdentity(input.UID, credential, h.now()); err != nil {
		switch {
		case errors.Is(err, errLGPaymentwallUnauthorized):
			http.Error(w, "License identity is already registered", http.StatusConflict)
		case errors.Is(err, errLGPaymentwallStateFull):
			http.Error(w, "Payment service is unavailable", http.StatusServiceUnavailable)
		default:
			http.Error(w, "Payment service is unavailable", http.StatusServiceUnavailable)
		}
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (h *lgPaymentwallHandler) serveCheckoutSession(w http.ResponseWriter, r *http.Request) {
	if h.prepareClientRequest(w, r, "POST") {
		return
	}
	if h.disabled(w) {
		return
	}
	if _, ok := h.admit(w, r, "checkout", ""); !ok {
		return
	}
	credential, ok := h.authenticateCredential(w, r)
	if !ok {
		return
	}
	uid := r.Header.Get("X-Pleazyng-LG-UID")
	if !lgPaymentwallUIDPattern.MatchString(uid) {
		http.Error(w, "Invalid license identity", http.StatusBadRequest)
		return
	}
	if !h.uidRate.allow("checkout:"+uid, h.now()) {
		markRateLimit(w, "Too many checkout requests", time.Second)
		return
	}
	if !h.store.authorized(uid, credential) {
		http.Error(w, "License identity is not authorized", http.StatusUnauthorized)
		return
	}
	sessionID, err := h.store.createCheckoutSession(uid, h.now())
	if err != nil {
		http.Error(w, "Payment service is unavailable", http.StatusServiceUnavailable)
		return
	}
	checkoutURL := *h.config.publicBaseURL
	checkoutURL.Path = lgPaymentwallCheckoutPath + "/" + sessionID
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(lgPaymentwallCheckoutResponse{URL: checkoutURL.String()})
}

func (h *lgPaymentwallHandler) serveCheckoutPage(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if h.disabled(w) {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if rejectLGPaymentwallOrigin(w, r, h.config.allowedOrigins) {
		return
	}
	sessionID := strings.TrimPrefix(r.URL.Path, lgPaymentwallCheckoutPage)
	if strings.Contains(sessionID, "/") || !validLGPaymentwallSessionID(sessionID) {
		http.NotFound(w, r)
		return
	}
	uid, ok := h.store.checkoutSession(sessionID, h.now())
	if !ok {
		http.Error(w, "Checkout session expired", http.StatusGone)
		return
	}
	nonce, err := randomLGPaymentwallHex(18)
	if err != nil {
		http.Error(w, "Payment service is unavailable", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("X-Frame-Options", "DENY")
	w.Header().Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; script-src 'nonce-"+nonce+"' 'unsafe-eval' https://api.paymentwall.com; connect-src https://api.paymentwall.com https://*.paymentwall.com; img-src data: https://api.paymentwall.com https://*.paymentwall.com; style-src 'nonce-"+nonce+"' https://api.paymentwall.com https://*.paymentwall.com; style-src-attr 'unsafe-inline'; frame-src https://api.paymentwall.com https://*.paymentwall.com; form-action https://api.paymentwall.com https://*.paymentwall.com; font-src data: https://api.paymentwall.com https://*.paymentwall.com")
	data := lgPaymentwallCheckoutTemplateData{Nonce: nonce, ProjectKey: h.config.projectKey, UID: uid, ProductSKU: lgPaymentwallProductSKU}
	if err := lgPaymentwallCheckoutHTML.Execute(w, data); err != nil {
		return
	}
}

func (h *lgPaymentwallHandler) serveEntitlement(w http.ResponseWriter, r *http.Request) {
	if h.prepareClientRequest(w, r, "GET") {
		return
	}
	if h.disabled(w) {
		return
	}
	if _, ok := h.admit(w, r, "entitlement", ""); !ok {
		return
	}
	credential, ok := h.authenticateCredential(w, r)
	if !ok {
		return
	}
	uid := r.Header.Get("X-Pleazyng-LG-UID")
	if !lgPaymentwallUIDPattern.MatchString(uid) || !h.store.authorized(uid, credential) {
		http.Error(w, "License identity is not authorized", http.StatusUnauthorized)
		return
	}
	mutationFence, err := h.store.captureMutationFence(uid)
	if err != nil {
		http.Error(w, "Payment ownership could not be durably verified", http.StatusServiceUnavailable)
		return
	}
	statusQueryStartedAt := h.now().UTC()
	payments, err := h.statusAPI.queryByUID(r.Context(), uid)
	if err != nil {
		http.Error(w, "Payment status is temporarily unavailable", http.StatusBadGateway)
		return
	}
	snapshot, err := h.store.reconcileWithFence(uid, payments, mutationFence, statusQueryStartedAt, h.now())
	if err != nil {
		http.Error(w, "Payment ownership could not be durably verified", http.StatusServiceUnavailable)
		return
	}
	grant, err := h.signGrant(uid, snapshot, h.now())
	if err != nil {
		http.Error(w, "Payment service is unavailable", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(lgPaymentwallEntitlementResponse{Grant: grant})
}

func (h *lgPaymentwallHandler) servePingback(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if h == nil || (!h.config.enabled && !h.config.testPingbackEnabled) {
		h.disabled(w)
		return
	}
	if h.config.enabled && (h.store == nil || !h.store.healthy()) {
		http.Error(w, "Payment state store is unavailable", http.StatusServiceUnavailable)
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	ip, err := h.clientIPs.resolve(r)
	if err != nil || !isLGPaymentwallTrustedIP(ip) {
		http.Error(w, "Pingback source is not allowed", http.StatusForbidden)
		return
	}
	if !h.globalRate.allow("pingback", h.now()) {
		markRateLimit(w, "Too many pingbacks", time.Second)
		return
	}
	params, err := readLGPaymentwallPingbackParams(w, r)
	if err != nil {
		http.Error(w, "Invalid pingback", http.StatusBadRequest)
		return
	}
	if !h.verifyPingbackSignature(params) {
		http.Error(w, "Invalid pingback signature", http.StatusForbidden)
		return
	}
	uid := params.Get("uid")
	ref := params.Get("ref")
	goodsID := params.Get("goodsid")
	typeValue, err := strconv.Atoi(params.Get("type"))
	if err != nil || !lgPaymentwallUIDPattern.MatchString(uid) || !lgPaymentwallReferencePattern.MatchString(ref) || goodsID != lgPaymentwallProductSKU {
		http.Error(w, "Invalid pingback fields", http.StatusBadRequest)
		return
	}
	if testMarker := params.Get("is_test"); testMarker != "" {
		if testMarker != "1" {
			http.Error(w, "Invalid test pingback marker", http.StatusBadRequest)
			return
		}
		// Paymentwall documents is_test=1 for its Test Payment method and Test
		// Pingback Tool. A valid signed test event is acknowledged for technical
		// review, but never queries status or writes production ownership state.
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "OK")
		return
	}
	if !h.config.enabled {
		http.Error(w, "Payment service is not configured for live payments", http.StatusServiceUnavailable)
		return
	}
	switch typeValue {
	case 0, 201:
		if _, ok := h.admit(w, r, "positive-pingback", uid); !ok {
			return
		}
		if !h.store.identityExists(uid) {
			// A valid signed payment without a previously registered capability
			// must never create a client identity or grant path.
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, "OK")
			return
		}
		mutationFence, fenceErr := h.store.captureMutationFence(uid)
		if fenceErr != nil {
			http.Error(w, "Payment status is temporarily unavailable", http.StatusServiceUnavailable)
			return
		}
		statusQueryStartedAt := h.now().UTC()
		payment, found, queryErr := h.statusAPI.queryByReference(r.Context(), ref)
		if queryErr != nil {
			http.Error(w, "Payment status is temporarily unavailable", http.StatusServiceUnavailable)
			return
		}
		if !found || !paymentwallPaymentMatches(payment, uid, ref, lgPaymentwallProductSKU) {
			http.Error(w, "Payment status is not confirmed", http.StatusServiceUnavailable)
			return
		}
		applied, applyErr := h.store.applyVerifiedPositivePingbackWithFence(uid, ref, typeValue, mutationFence, statusQueryStartedAt, h.now())
		if applyErr != nil {
			http.Error(w, "Payment state could not be durably stored", http.StatusServiceUnavailable)
			return
		}
		if !applied {
			// This update follows a fresh, exact Status API response. A callback by
			// itself never reverses a refund or grants ownership.
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, "OK")
			return
		}
	case 200:
		if !h.store.identityExists(uid) {
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, "OK")
			return
		}
		applied, applyErr := h.store.recordNonGrantingEvent(uid, ref, typeValue, h.now())
		if applyErr != nil {
			http.Error(w, "Payment state could not be durably stored", http.StatusServiceUnavailable)
			return
		}
		if !applied {
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, "OK")
			return
		}
	case 2, 202, 203, 220:
		applied, applyErr := h.store.applyNegativePingback(uid, ref, typeValue, h.now())
		if applyErr != nil {
			http.Error(w, "Payment revocation could not be durably stored", http.StatusServiceUnavailable)
			return
		}
		if !applied {
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, "OK")
			return
		}
	case 1, 12, 13, 14:
		// Customer-service courtesy and subscription lifecycle events do not
		// grant this one-time product. They are acknowledged, never delivered.
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "OK")
		return
	default:
		// Unknown future types are acknowledged but cannot create entitlements.
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "OK")
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, "OK")
}

func (h *lgPaymentwallHandler) prepareClientRequest(w http.ResponseWriter, r *http.Request, methods string) bool {
	w.Header().Set("Cache-Control", "no-store")
	if rejectLGPaymentwallOrigin(w, r, h.config.allowedOrigins) {
		return true
	}
	origins := r.Header.Values("Origin")
	if len(origins) == 1 {
		w.Header().Set("Access-Control-Allow-Origin", origins[0])
		w.Header().Add("Vary", "Origin")
		w.Header().Set("Access-Control-Allow-Methods", methods+", OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Pleazyng-LG-UID")
		w.Header().Set("Access-Control-Max-Age", "600")
	}
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return true
	}
	if r.Method != methods {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return true
	}
	return false
}

func (h *lgPaymentwallHandler) disabled(w http.ResponseWriter) bool {
	if h != nil && h.config.enabled && h.store != nil && h.store.healthy() && h.statusAPI != nil {
		return false
	}
	http.Error(w, "Payment service is not configured", http.StatusServiceUnavailable)
	return true
}

func (h *lgPaymentwallHandler) admit(w http.ResponseWriter, r *http.Request, scope, uid string) (string, bool) {
	ip, err := h.clientIPs.resolve(r)
	if err != nil {
		http.Error(w, "Invalid client address", http.StatusBadRequest)
		return "", false
	}
	now := h.now()
	if !h.ipRate.allow(ip, now) || (uid != "" && !h.uidRate.allow(uid, now)) {
		markRateLimit(w, "Too many payment requests", time.Second)
		return "", false
	}
	if scope == "checkout" && !h.uidRate.allow("checkout:"+uid, now) {
		markRateLimit(w, "Too many checkout requests", time.Second)
		return "", false
	}
	return ip, true
}

func (h *lgPaymentwallHandler) authenticateCredential(w http.ResponseWriter, r *http.Request) (string, bool) {
	values := r.Header.Values("Authorization")
	if len(values) != 1 {
		http.Error(w, "Payment identity required", http.StatusUnauthorized)
		return "", false
	}
	parts := strings.SplitN(values[0], " ", 2)
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || !lgPaymentwallCredentialPattern.MatchString(parts[1]) {
		http.Error(w, "Payment identity required", http.StatusUnauthorized)
		return "", false
	}
	return parts[1], true
}

func requireJSON(w http.ResponseWriter, r *http.Request) bool {
	mediaType, _, err := mimeParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || mediaType != "application/json" {
		http.Error(w, "Expected JSON", http.StatusUnsupportedMediaType)
		return false
	}
	return true
}

func decodeLGPaymentwallJSON(w http.ResponseWriter, r *http.Request, output any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, lgPaymentwallMaxBodyBytes)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(output); err != nil {
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return false
	}
	var trailing any
	if !errors.Is(decoder.Decode(&trailing), io.EOF) {
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return false
	}
	return true
}

func (h *lgPaymentwallHandler) verifyPingbackSignature(params url.Values) bool {
	version := params.Get("sign_version")
	if version != "3" && !(version == "2" && h.config.allowSignatureV2) {
		return false
	}
	provided := strings.ToLower(params.Get("sig"))
	calculated, err := paymentwallSignature(params, h.config.secretKey, version, "sig")
	if err != nil || len(provided) != len(calculated) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(provided), []byte(calculated)) == 1
}

func paymentwallSignature(params url.Values, secret, version, signatureField string) (string, error) {
	keys := make([]string, 0, len(params))
	for key, values := range params {
		if key == signatureField || key == "sign" || key == "sig" {
			continue
		}
		if key == "" || len(values) != 1 {
			return "", errors.New("ambiguous Paymentwall signature parameters")
		}
		keys = append(keys, key)
	}
	sort.Strings(keys)
	var base strings.Builder
	for _, key := range keys {
		base.WriteString(key)
		base.WriteByte('=')
		base.WriteString(params.Get(key))
	}
	base.WriteString(secret)
	input := []byte(base.String())
	switch version {
	case "2":
		sum := md5.Sum(input)
		return hex.EncodeToString(sum[:]), nil
	case "3":
		sum := sha256.Sum256(input)
		return hex.EncodeToString(sum[:]), nil
	default:
		return "", errors.New("unsupported Paymentwall signature version")
	}
}

func (h *lgPaymentwallHandler) signGrant(uid string, snapshot lgPaymentwallOwnershipSnapshot, now time.Time) (string, error) {
	payload := lgPaymentwallGrantPayload{
		Version:     lgPaymentwallGrantVersion,
		UID:         uid,
		ProductID:   lgPaymentwallProductSKU,
		Currency:    lgPaymentwallCurrency,
		AmountCents: lgPaymentwallAmountCents,
		Owned:       snapshot.Owned,
		Revision:    snapshot.Revision,
		IssuedAt:    now.UTC(),
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	signature := ed25519.Sign(h.config.grantPrivateKey, body)
	return base64.RawURLEncoding.EncodeToString(body) + "." + base64.RawURLEncoding.EncodeToString(signature), nil
}

func (c *lgPaymentwallStatusAPI) queryByReference(ctx context.Context, ref string) (lgPaymentwallPayment, bool, error) {
	params := url.Values{
		"key":          {c.projectKey},
		"ref":          {ref},
		"sign_version": {"3"},
	}
	signature, err := paymentwallSignature(params, c.secretKey, "3", "sign")
	if err != nil {
		return lgPaymentwallPayment{}, false, err
	}
	params.Set("sign", signature)
	payments, err := c.query(ctx, params)
	if err != nil {
		return lgPaymentwallPayment{}, false, err
	}
	var found *lgPaymentwallPayment
	for _, payment := range payments {
		if payment.ID == ref {
			if found != nil {
				return lgPaymentwallPayment{}, false, errLGPaymentwallStatusUnavailable
			}
			copy := payment
			found = &copy
		}
	}
	if found == nil {
		return lgPaymentwallPayment{}, false, nil
	}
	return *found, true, nil
}

func (c *lgPaymentwallStatusAPI) queryByUID(ctx context.Context, uid string) ([]lgPaymentwallPayment, error) {
	params := url.Values{
		"key":            {c.projectKey},
		"uid":            {uid},
		"ag_external_id": {lgPaymentwallProductSKU},
		"sign_version":   {"3"},
	}
	signature, err := paymentwallSignature(params, c.secretKey, "3", "sign")
	if err != nil {
		return nil, err
	}
	params.Set("sign", signature)
	payments, err := c.query(ctx, params)
	if err != nil {
		return nil, err
	}
	for _, payment := range payments {
		if payment.UID != uid || payment.ProductID != lgPaymentwallProductSKU {
			return nil, errLGPaymentwallStatusUnavailable
		}
	}
	return payments, nil
}

func (c *lgPaymentwallStatusAPI) query(ctx context.Context, params url.Values) ([]lgPaymentwallPayment, error) {
	endpoint, err := url.Parse(c.endpoint)
	if err != nil || endpoint.Scheme != "https" || endpoint.Host != "api.paymentwall.com" || endpoint.Path != "/api/rest/payment/" {
		return nil, errors.New("invalid Paymentwall status endpoint")
	}
	endpoint.RawQuery = params.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint.String(), nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept", "application/json")
	response, err := c.client.Do(request)
	if err != nil {
		return nil, errLGPaymentwallStatusUnavailable
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, errLGPaymentwallStatusUnavailable
	}
	mediaType, _, err := mime.ParseMediaType(response.Header.Get("Content-Type"))
	// The legacy Status API labels JSON payment histories as text/html.
	// The endpoint is pinned above and the body must still be a valid payment list.
	if err != nil || (mediaType != "application/json" && mediaType != "text/html") {
		return nil, errLGPaymentwallStatusUnavailable
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 256*1024+1))
	if err != nil || len(data) > 256*1024 {
		return nil, errLGPaymentwallStatusUnavailable
	}
	var payments []lgPaymentwallPayment
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(&payments); err != nil {
		return nil, errLGPaymentwallStatusUnavailable
	}
	var trailing any
	if !errors.Is(decoder.Decode(&trailing), io.EOF) || payments == nil || len(payments) > 2000 {
		return nil, errLGPaymentwallStatusUnavailable
	}
	seen := make(map[string]struct{}, len(payments))
	for _, payment := range payments {
		if payment.Object != "payment" || !lgPaymentwallReferencePattern.MatchString(payment.ID) || payment.UID == "" ||
			payment.Amount == "" || payment.Currency == "" || payment.Refunded == nil || payment.Risk == "" {
			return nil, errLGPaymentwallStatusUnavailable
		}
		if _, duplicate := seen[payment.ID]; duplicate {
			return nil, errLGPaymentwallStatusUnavailable
		}
		seen[payment.ID] = struct{}{}
	}
	return payments, nil
}

func paymentwallPaymentMatches(payment lgPaymentwallPayment, uid, ref, productID string) bool {
	return payment.Object == "payment" && payment.ID == ref && payment.UID == uid && payment.ProductID == productID &&
		payment.Amount == "2.99" && payment.Currency == lgPaymentwallCurrency && payment.Refunded != nil &&
		!*payment.Refunded && payment.Risk == "approved"
}

func paymentwallPaymentOwns(payment lgPaymentwallPayment, uid string) bool {
	return payment.Object == "payment" && lgPaymentwallReferencePattern.MatchString(payment.ID) && payment.UID == uid &&
		payment.ProductID == lgPaymentwallProductSKU && payment.Amount == "2.99" &&
		payment.Currency == lgPaymentwallCurrency && payment.Refunded != nil && !*payment.Refunded &&
		payment.Risk == "approved"
}

func (s *lgPaymentwallStore) registerIdentity(uid, credential string, now time.Time) error {
	if !lgPaymentwallUIDPattern.MatchString(uid) || !lgPaymentwallCredentialPattern.MatchString(credential) {
		return errLGPaymentwallUnauthorized
	}
	digest := sha256.Sum256([]byte(credential))
	digestText := hex.EncodeToString(digest[:])
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.poisoned {
		return errLGPaymentwallStorePoisoned
	}
	if existing, ok := s.state.Identities[uid]; ok {
		if subtle.ConstantTimeCompare([]byte(existing.CredentialHash), []byte(digestText)) != 1 {
			return errLGPaymentwallUnauthorized
		}
		return nil
	}
	if len(s.state.Identities) >= lgPaymentwallMaxIdentities {
		return errLGPaymentwallStateFull
	}
	next := cloneLGPaymentwallState(s.state)
	next.Identities[uid] = lgPaymentwallIdentity{CredentialHash: digestText, CreatedAt: now.UTC()}
	return s.commitLocked(next)
}

func (s *lgPaymentwallStore) authorized(uid, credential string) bool {
	digest := sha256.Sum256([]byte(credential))
	digestText := hex.EncodeToString(digest[:])
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.poisoned {
		return false
	}
	identity, ok := s.state.Identities[uid]
	return ok && subtle.ConstantTimeCompare([]byte(identity.CredentialHash), []byte(digestText)) == 1
}

func (s *lgPaymentwallStore) identityExists(uid string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.poisoned {
		return false
	}
	_, ok := s.state.Identities[uid]
	return ok
}

func (s *lgPaymentwallStore) captureMutationFence(uid string) (lgPaymentwallMutationFence, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.poisoned {
		return nil, errLGPaymentwallStorePoisoned
	}
	if _, ok := s.state.Identities[uid]; !ok {
		return nil, errLGPaymentwallUnauthorized
	}
	fence := make(lgPaymentwallMutationFence)
	for ref, reference := range s.state.References {
		if reference.UID == uid {
			fence[ref] = reference.Mutation
		}
	}
	return fence, nil
}

func referenceChangedSinceFence(fence lgPaymentwallMutationFence, references map[string]lgPaymentwallReference, ref string) bool {
	reference, exists := references[ref]
	if !exists {
		return false
	}
	fencedMutation, wasPresent := fence[ref]
	return !wasPresent || reference.Mutation != fencedMutation
}

func advanceLGPaymentwallReferenceMutation(reference *lgPaymentwallReference) error {
	if reference.Mutation == ^uint64(0) {
		return errLGPaymentwallInvalidState
	}
	reference.Mutation++
	return nil
}

func (s *lgPaymentwallStore) createCheckoutSession(uid string, now time.Time) (string, error) {
	token, err := randomLGPaymentwallHex(32)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256([]byte(token))
	digestText := hex.EncodeToString(digest[:])
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.poisoned {
		return "", errLGPaymentwallStorePoisoned
	}
	if _, ok := s.state.Identities[uid]; !ok {
		return "", errLGPaymentwallUnauthorized
	}
	next := cloneLGPaymentwallState(s.state)
	for key, session := range next.Sessions {
		if !session.ExpiresAt.After(now) {
			delete(next.Sessions, key)
		}
	}
	if len(next.Sessions) >= lgPaymentwallMaxCheckoutPages {
		return "", errLGPaymentwallStateFull
	}
	next.Sessions[digestText] = lgPaymentwallCheckoutSession{UID: uid, ExpiresAt: now.Add(lgPaymentwallCheckoutSessionTTL).UTC()}
	if err := s.commitLocked(next); err != nil {
		return "", err
	}
	return token, nil
}

func (s *lgPaymentwallStore) checkoutSession(token string, now time.Time) (string, bool) {
	digest := sha256.Sum256([]byte(token))
	digestText := hex.EncodeToString(digest[:])
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.poisoned {
		return "", false
	}
	session, ok := s.state.Sessions[digestText]
	if !ok || !session.ExpiresAt.After(now) {
		if ok {
			next := cloneLGPaymentwallState(s.state)
			delete(next.Sessions, digestText)
			_ = s.commitLocked(next)
		}
		return "", false
	}
	return session.UID, true
}

// applyVerifiedPositivePingback must only be called after a fresh Paymentwall
// Status API response matched the UID, reference, SKU, amount, currency, risk,
// and refund state. It can reactivate a reference after a refund reversal only
// on that authoritative path; the pingback alone is not proof.
func (s *lgPaymentwallStore) applyVerifiedPositivePingback(uid, ref string, eventType int, proofStartedAt, now time.Time) (bool, error) {
	fence, err := s.captureMutationFence(uid)
	if err != nil {
		if errors.Is(err, errLGPaymentwallUnauthorized) {
			return false, nil
		}
		return false, err
	}
	return s.applyVerifiedPositivePingbackWithFence(uid, ref, eventType, fence, proofStartedAt, now)
}

func (s *lgPaymentwallStore) applyVerifiedPositivePingbackWithFence(uid, ref string, eventType int, fence lgPaymentwallMutationFence, proofStartedAt, now time.Time) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.poisoned {
		return false, errLGPaymentwallStorePoisoned
	}
	if existing, ok := s.state.References[ref]; ok {
		if existing.UID != uid || existing.SKU != lgPaymentwallProductSKU {
			return false, nil
		}
		if referenceChangedSinceFence(fence, s.state.References, ref) {
			return false, nil
		}
		if existing.Revoked && existing.UpdatedAt.After(proofStartedAt) {
			return false, nil
		}
	} else if referenceChangedSinceFence(fence, s.state.References, ref) {
		return false, nil
	}
	eventKey := lgPaymentwallEventKey(ref, eventType)
	_, duplicate := s.state.Events[eventKey]
	next := cloneLGPaymentwallState(s.state)
	if (!duplicate && len(next.Events) >= lgPaymentwallMaxEvents) || (len(next.References) >= lgPaymentwallMaxReferences && next.References[ref].UID == "") {
		return false, errLGPaymentwallStateFull
	}
	reference := next.References[ref]
	reference.UID = uid
	reference.SKU = lgPaymentwallProductSKU
	reference.Verified = true
	reference.Revoked = false
	if err := advanceLGPaymentwallReferenceMutation(&reference); err != nil {
		return false, err
	}
	reference.UpdatedAt = now.UTC()
	next.References[ref] = reference
	if !duplicate {
		next.Events[eventKey] = lgPaymentwallEvent{UID: uid, Ref: ref, Type: eventType, ReceivedAt: now.UTC()}
	}
	if identity, ok := next.Identities[uid]; ok {
		setLGPaymentwallOwned(&identity, hasLGPaymentwallPurchase(next, uid), now)
		next.Identities[uid] = identity
	}
	if err := s.commitLocked(next); err != nil {
		return false, err
	}
	return true, nil
}

func (s *lgPaymentwallStore) recordNonGrantingEvent(uid, ref string, eventType int, now time.Time) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.poisoned {
		return false, errLGPaymentwallStorePoisoned
	}
	if _, duplicate := s.state.Events[lgPaymentwallEventKey(ref, eventType)]; duplicate {
		return true, nil
	}
	if len(s.state.Events) >= lgPaymentwallMaxEvents {
		return false, errLGPaymentwallStateFull
	}
	next := cloneLGPaymentwallState(s.state)
	next.Events[lgPaymentwallEventKey(ref, eventType)] = lgPaymentwallEvent{UID: uid, Ref: ref, Type: eventType, ReceivedAt: now.UTC()}
	if err := s.commitLocked(next); err != nil {
		return false, err
	}
	return true, nil
}

func (s *lgPaymentwallStore) applyNegativePingback(uid, ref string, eventType int, now time.Time) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.poisoned {
		return false, errLGPaymentwallStorePoisoned
	}
	if existing, ok := s.state.References[ref]; ok && existing.UID != uid {
		return false, nil
	}
	eventKey := lgPaymentwallEventKey(ref, eventType)
	previousEvent, duplicate := s.state.Events[eventKey]
	if duplicate && previousEvent.UID != uid {
		return false, nil
	}
	next := cloneLGPaymentwallState(s.state)
	if (!duplicate && len(next.Events) >= lgPaymentwallMaxEvents) || (len(next.References) >= lgPaymentwallMaxReferences && next.References[ref].UID == "") {
		return false, errLGPaymentwallStateFull
	}
	reference := next.References[ref]
	reference.UID = uid
	reference.SKU = lgPaymentwallProductSKU
	reference.Revoked = true
	reference.Verified = false
	if err := advanceLGPaymentwallReferenceMutation(&reference); err != nil {
		return false, err
	}
	reference.UpdatedAt = now.UTC()
	next.References[ref] = reference
	if !duplicate {
		next.Events[eventKey] = lgPaymentwallEvent{UID: uid, Ref: ref, Type: eventType, ReceivedAt: now.UTC()}
	}
	if identity, ok := next.Identities[uid]; ok {
		setLGPaymentwallOwned(&identity, hasLGPaymentwallPurchase(next, uid), now)
		next.Identities[uid] = identity
	}
	if err := s.commitLocked(next); err != nil {
		return false, err
	}
	return true, nil
}

func (s *lgPaymentwallStore) reconcile(uid string, payments []lgPaymentwallPayment, proofStartedAt, now time.Time) (lgPaymentwallOwnershipSnapshot, error) {
	fence, err := s.captureMutationFence(uid)
	if err != nil {
		return lgPaymentwallOwnershipSnapshot{}, err
	}
	return s.reconcileWithFence(uid, payments, fence, proofStartedAt, now)
}

func (s *lgPaymentwallStore) reconcileWithFence(uid string, payments []lgPaymentwallPayment, fence lgPaymentwallMutationFence, proofStartedAt, now time.Time) (lgPaymentwallOwnershipSnapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.poisoned {
		return lgPaymentwallOwnershipSnapshot{}, errLGPaymentwallStorePoisoned
	}
	identity, ok := s.state.Identities[uid]
	if !ok {
		return lgPaymentwallOwnershipSnapshot{}, errLGPaymentwallUnauthorized
	}
	next := cloneLGPaymentwallState(s.state)
	active := make(map[string]bool)
	for _, payment := range payments {
		if !lgPaymentwallReferencePattern.MatchString(payment.ID) || payment.UID != uid || payment.ProductID != lgPaymentwallProductSKU {
			continue
		}
		if referenceChangedSinceFence(fence, next.References, payment.ID) {
			continue
		}
		if paymentwallPaymentOwns(payment, uid) {
			if reference, exists := next.References[payment.ID]; exists {
				if reference.UID != uid || reference.UpdatedAt.After(proofStartedAt) {
					continue
				}
			}
			active[payment.ID] = true
			reference := next.References[payment.ID]
			reference.UID = uid
			reference.SKU = lgPaymentwallProductSKU
			reference.Verified = true
			reference.Revoked = false
			if err := advanceLGPaymentwallReferenceMutation(&reference); err != nil {
				return lgPaymentwallOwnershipSnapshot{}, err
			}
			reference.UpdatedAt = now.UTC()
			next.References[payment.ID] = reference
		} else if payment.Refunded != nil && *payment.Refunded {
			reference := next.References[payment.ID]
			if (reference.UID == "" || reference.UID == uid) && !reference.UpdatedAt.After(proofStartedAt) {
				reference.UID = uid
				reference.SKU = lgPaymentwallProductSKU
				reference.Revoked = true
				reference.Verified = false
				if err := advanceLGPaymentwallReferenceMutation(&reference); err != nil {
					return lgPaymentwallOwnershipSnapshot{}, err
				}
				reference.UpdatedAt = now.UTC()
				next.References[payment.ID] = reference
			}
		}
	}
	for ref, reference := range next.References {
		if reference.UID != uid || !reference.Verified || reference.Revoked || reference.UpdatedAt.After(proofStartedAt) || referenceChangedSinceFence(fence, next.References, ref) {
			continue
		}
		if _, present := active[ref]; !present {
			reference.Verified = false
			reference.Revoked = true
			if err := advanceLGPaymentwallReferenceMutation(&reference); err != nil {
				return lgPaymentwallOwnershipSnapshot{}, err
			}
			reference.UpdatedAt = now.UTC()
			next.References[ref] = reference
		}
	}
	// A refund pingback remains effective while the Status API reports it
	// refunded. A fresh approved, unrefunded Status API result may reactivate
	// that reference if Paymentwall later reverses the refund.
	owned := hasLGPaymentwallPurchase(next, uid)
	setLGPaymentwallOwned(&identity, owned, now)
	next.Identities[uid] = identity
	if !lgPaymentwallStatesEqual(s.state, next) {
		if err := s.commitLocked(next); err != nil {
			return lgPaymentwallOwnershipSnapshot{}, err
		}
	}
	committed := s.state.Identities[uid]
	return lgPaymentwallOwnershipSnapshot{Owned: committed.Owned, Revision: committed.Revision}, nil
}

func hasLGPaymentwallPurchase(state lgPaymentwallState, uid string) bool {
	for _, reference := range state.References {
		if reference.UID == uid && reference.SKU == lgPaymentwallProductSKU && reference.Verified && !reference.Revoked {
			return true
		}
	}
	return false
}

func setLGPaymentwallOwned(identity *lgPaymentwallIdentity, owned bool, now time.Time) {
	if identity.Owned != owned {
		identity.Owned = owned
		identity.Revision++
	}
	_ = now
}

func lgPaymentwallStatesEqual(left, right lgPaymentwallState) bool {
	a, errA := json.Marshal(left)
	b, errB := json.Marshal(right)
	return errA == nil && errB == nil && bytes.Equal(a, b)
}

func lgPaymentwallEventKey(ref string, eventType int) string {
	return ref + ":" + strconv.Itoa(eventType)
}

func newLGPaymentwallStore(path string) (*lgPaymentwallStore, error) {
	if path == "" {
		return nil, errLGPaymentwallInvalidState
	}
	lockPath := path + ".lock"
	if info, err := os.Lstat(lockPath); err == nil && (!info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0) {
		return nil, errLGPaymentwallInvalidState
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	lockFile, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err := lockFile.Chmod(0600); err != nil {
		_ = lockFile.Close()
		return nil, err
	}
	if err := lockLGPaymentwallFile(lockFile); err != nil {
		_ = lockFile.Close()
		return nil, err
	}
	store := &lgPaymentwallStore{path: path, lockFile: lockFile, syncDir: syncLGPaymentwallDirectory, state: emptyLGPaymentwallState()}
	fail := func(err error) (*lgPaymentwallStore, error) {
		_ = unlockLGPaymentwallFile(lockFile)
		_ = lockFile.Close()
		return nil, err
	}
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		if err := store.persist(store.state); err != nil {
			return fail(err)
		}
		return store, nil
	}
	if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0077 != 0 || info.Size() < 1 || info.Size() > lgPaymentwallMaxStateBytes {
		return fail(errLGPaymentwallInvalidState)
	}
	data, err := os.ReadFile(path)
	if err != nil || len(data) > lgPaymentwallMaxStateBytes {
		return fail(errLGPaymentwallInvalidState)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var state lgPaymentwallState
	if err := decoder.Decode(&state); err != nil {
		return fail(errLGPaymentwallInvalidState)
	}
	var trailing any
	if !errors.Is(decoder.Decode(&trailing), io.EOF) || !validateLGPaymentwallState(state) {
		return fail(errLGPaymentwallInvalidState)
	}
	store.state = state
	return store, nil
}

func (s *lgPaymentwallStore) close() error {
	if s == nil || s.lockFile == nil {
		return nil
	}
	err := unlockLGPaymentwallFile(s.lockFile)
	closeErr := s.lockFile.Close()
	s.lockFile = nil
	return errors.Join(err, closeErr)
}

func emptyLGPaymentwallState() lgPaymentwallState {
	return lgPaymentwallState{Version: lgPaymentwallStateVersion, Identities: map[string]lgPaymentwallIdentity{}, References: map[string]lgPaymentwallReference{}, Events: map[string]lgPaymentwallEvent{}, Sessions: map[string]lgPaymentwallCheckoutSession{}}
}

func validateLGPaymentwallState(state lgPaymentwallState) bool {
	if state.Version != lgPaymentwallStateVersion || state.Identities == nil || state.References == nil || state.Events == nil || state.Sessions == nil ||
		len(state.Identities) > lgPaymentwallMaxIdentities || len(state.References) > lgPaymentwallMaxReferences || len(state.Events) > lgPaymentwallMaxEvents || len(state.Sessions) > lgPaymentwallMaxCheckoutPages {
		return false
	}
	for uid, identity := range state.Identities {
		if !lgPaymentwallUIDPattern.MatchString(uid) || len(identity.CredentialHash) != 64 || !isHexString(identity.CredentialHash) || identity.CreatedAt.IsZero() {
			return false
		}
	}
	for ref, record := range state.References {
		if !lgPaymentwallReferencePattern.MatchString(ref) || !lgPaymentwallUIDPattern.MatchString(record.UID) || record.SKU != lgPaymentwallProductSKU || record.UpdatedAt.IsZero() || (record.Verified && record.Revoked) {
			return false
		}
	}
	for key, event := range state.Events {
		if !lgPaymentwallReferencePattern.MatchString(event.Ref) || key != lgPaymentwallEventKey(event.Ref, event.Type) || !lgPaymentwallUIDPattern.MatchString(event.UID) || event.ReceivedAt.IsZero() {
			return false
		}
	}
	for digest, session := range state.Sessions {
		if len(digest) != 64 || !isHexString(digest) || !lgPaymentwallUIDPattern.MatchString(session.UID) || session.ExpiresAt.IsZero() {
			return false
		}
	}
	return true
}

func cloneLGPaymentwallState(source lgPaymentwallState) lgPaymentwallState {
	clone := lgPaymentwallState{Version: source.Version, Identities: make(map[string]lgPaymentwallIdentity, len(source.Identities)), References: make(map[string]lgPaymentwallReference, len(source.References)), Events: make(map[string]lgPaymentwallEvent, len(source.Events)), Sessions: make(map[string]lgPaymentwallCheckoutSession, len(source.Sessions))}
	for key, value := range source.Identities {
		clone.Identities[key] = value
	}
	for key, value := range source.References {
		clone.References[key] = value
	}
	for key, value := range source.Events {
		clone.Events[key] = value
	}
	for key, value := range source.Sessions {
		clone.Sessions[key] = value
	}
	return clone
}

func (s *lgPaymentwallStore) commitLocked(next lgPaymentwallState) error {
	if s.poisoned {
		return errLGPaymentwallStorePoisoned
	}
	if !validateLGPaymentwallState(next) {
		return errLGPaymentwallInvalidState
	}
	if err := s.persist(next); err != nil {
		return err
	}
	s.state = next
	return nil
}

func (s *lgPaymentwallStore) persist(state lgPaymentwallState) error {
	data, err := json.Marshal(state)
	if err != nil || len(data) > lgPaymentwallMaxStateBytes {
		return errLGPaymentwallStateFull
	}
	dir := filepath.Dir(s.path)
	file, err := os.CreateTemp(dir, ".lg-paymentwall-*.tmp")
	if err != nil {
		return err
	}
	tmpPath := file.Name()
	removeTemp := true
	defer func() {
		if removeTemp {
			_ = os.Remove(tmpPath)
		}
	}()
	if err := file.Chmod(0600); err != nil {
		_ = file.Close()
		return err
	}
	if _, err := file.Write(data); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpPath, s.path); err != nil {
		return err
	}
	removeTemp = false
	if err := s.syncDir(dir); err != nil {
		s.poisoned = true
		return err
	}
	return nil
}

func syncLGPaymentwallDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return err
	}
	syncErr := directory.Sync()
	closeErr := directory.Close()
	return errors.Join(syncErr, closeErr)
}

func (s *lgPaymentwallStore) healthy() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return !s.poisoned
}

func (g *lgPaymentwallRateGate) allow(key string, now time.Time) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	cutoff := now.Add(-g.window)
	active := make([]time.Time, 0, len(g.hits[key])+1)
	for _, hit := range g.hits[key] {
		if hit.After(cutoff) {
			active = append(active, hit)
		}
	}
	if len(active) >= g.limit {
		g.hits[key] = active
		return false
	}
	if _, exists := g.hits[key]; !exists {
		for candidate, hits := range g.hits {
			if len(hits) == 0 || !hits[len(hits)-1].After(cutoff) {
				delete(g.hits, candidate)
			}
		}
		if len(g.hits) >= g.maxKeys {
			return false
		}
	}
	active = append(active, now)
	g.hits[key] = active
	return true
}

func newLGPaymentwallRateGate(limit int, window time.Duration, maxKeys int) *lgPaymentwallRateGate {
	return &lgPaymentwallRateGate{limit: limit, window: window, maxKeys: maxKeys, hits: make(map[string][]time.Time)}
}

func readLGPaymentwallPingbackParams(w http.ResponseWriter, r *http.Request) (url.Values, error) {
	var params url.Values
	if r.Method == http.MethodGet {
		parsed, err := url.ParseQuery(r.URL.RawQuery)
		if err != nil || len(r.URL.RawQuery) > lgPaymentwallMaxBodyBytes {
			return nil, errLGPaymentwallInvalidState
		}
		params = parsed
	} else {
		mediaType, _, err := mimeParseMediaType(r.Header.Get("Content-Type"))
		if err != nil || mediaType != "application/x-www-form-urlencoded" {
			return nil, errLGPaymentwallInvalidState
		}
		r.Body = http.MaxBytesReader(w, r.Body, lgPaymentwallMaxBodyBytes)
		if err := r.ParseForm(); err != nil {
			return nil, errLGPaymentwallInvalidState
		}
		if len(r.URL.RawQuery) != 0 {
			return nil, errLGPaymentwallInvalidState
		}
		params = r.PostForm
	}
	if len(params) < 6 || len(params) > 64 {
		return nil, errLGPaymentwallInvalidState
	}
	for key, values := range params {
		if key == "" || len(key) > 128 || len(values) != 1 || len(values[0]) > 1024 {
			return nil, errLGPaymentwallInvalidState
		}
	}
	return params, nil
}

func isLGPaymentwallTrustedIP(raw string) bool {
	addr, err := netip.ParseAddr(raw)
	if err != nil {
		return false
	}
	addr = addr.Unmap()
	for _, network := range lgPaymentwallTrustedPingbackNetworks {
		if network.Contains(addr) {
			return true
		}
	}
	return false
}

func rejectLGPaymentwallOrigin(w http.ResponseWriter, r *http.Request, allowed map[string]struct{}) bool {
	origins := r.Header.Values("Origin")
	if len(origins) == 0 {
		return false
	}
	if len(origins) != 1 || !isLGPaymentwallAllowedOrigin(origins[0], allowed) {
		http.Error(w, "Origin not allowed", http.StatusForbidden)
		return true
	}
	return false
}

func isLGPaymentwallAllowedOrigin(value string, allowed map[string]struct{}) bool {
	if len(value) > 1024 || strings.TrimSpace(value) != value {
		return false
	}
	parsed, err := url.Parse(value)
	if err != nil || parsed.User != nil || parsed.Host == "" || parsed.Path != "" || parsed.RawQuery != "" || parsed.Fragment != "" || parsed.Scheme != "https" {
		return false
	}
	_, ok := allowed[strings.ToLower(parsed.Scheme)+"://"+strings.ToLower(parsed.Host)]
	return ok
}

func parseLGPaymentwallOrigins(raw string) (map[string]struct{}, error) {
	origins := make(map[string]struct{})
	for _, value := range strings.Split(raw, ",") {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		parsed, err := url.Parse(value)
		if err != nil || parsed.User != nil || parsed.Host == "" || parsed.Path != "" || parsed.RawQuery != "" || parsed.Fragment != "" || parsed.Scheme != "https" {
			return nil, errors.New("invalid Paymentwall origin allowlist")
		}
		origins[strings.ToLower(parsed.Scheme)+"://"+strings.ToLower(parsed.Host)] = struct{}{}
	}
	if len(origins) == 0 {
		return nil, errors.New("Paymentwall origin allowlist is empty")
	}
	return origins, nil
}

func decodeLGPaymentwallPrivateKey(raw string) (ed25519.PrivateKey, error) {
	decoded, err := base64.RawURLEncoding.DecodeString(raw)
	if err != nil {
		decoded, err = base64.StdEncoding.DecodeString(raw)
	}
	if err != nil {
		return nil, err
	}
	switch len(decoded) {
	case ed25519.SeedSize:
		return ed25519.NewKeyFromSeed(decoded), nil
	case ed25519.PrivateKeySize:
		return ed25519.PrivateKey(decoded), nil
	default:
		return nil, errors.New("invalid Ed25519 key length")
	}
}

func randomLGPaymentwallHex(byteCount int) (string, error) {
	if byteCount < 16 || byteCount > 64 {
		return "", errors.New("invalid random token size")
	}
	bytes := make([]byte, byteCount)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return hex.EncodeToString(bytes), nil
}

func validLGPaymentwallSessionID(value string) bool {
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func isHexString(value string) bool {
	if value == "" {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func mimeParseMediaType(value string) (string, map[string]string, error) {
	return mime.ParseMediaType(value)
}
