package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

const (
	testLGUID        = "0123456789abcdef0123456789abcdef"
	testLGCredential = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	testLGSecret     = "shared_secret"
)

// Fixture schema follows Paymentwall's Payment Status API response sample:
// object, id, amount, currency, refunded, risk, uid, and product_id. Source:
// https://www.paymentwall.com/en/documentation/Payment-Status-API/3019
const paymentwallApprovedFixture = `[{"object":"payment","id":"ref123","created":1419438832,"amount":"2.99","currency":"USD","refunded":false,"risk":"approved","uid":"0123456789abcdef0123456789abcdef","product_id":"pleazyng_lifetime_unlock","payment_system":"cc"}]`

type lgRoundTripper func(*http.Request) (*http.Response, error)

func (f lgRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func newLGTestStore(t *testing.T) *lgPaymentwallStore {
	t.Helper()
	store, err := newLGPaymentwallStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("newLGPaymentwallStore: %v", err)
	}
	t.Cleanup(func() { _ = store.close() })
	return store
}

func TestPaymentwallSignatureV3MatchesSHA256Vector(t *testing.T) {
	params := url.Values{
		"goodsid":      {lgPaymentwallProductSKU},
		"ref":          {"ref123"},
		"sign_version": {"3"},
		"type":         {"0"},
		"uid":          {testLGUID},
	}
	signature, err := paymentwallSignature(params, testLGSecret, "3", "sig")
	if err != nil {
		t.Fatal(err)
	}
	const expected = "ccd4685efa187261c079c366377bd22d33c6efbedcecd1c8ad3e32f10068b9ed"
	if signature != expected {
		t.Fatalf("signature = %s, want %s", signature, expected)
	}
}

func TestPaymentwallSignatureV2RequiresExplicitConfig(t *testing.T) {
	params := url.Values{
		"goodsid":      {lgPaymentwallProductSKU},
		"ref":          {"ref123"},
		"sign_version": {"2"},
		"type":         {"0"},
		"uid":          {testLGUID},
	}
	signature, err := paymentwallSignature(params, testLGSecret, "2", "sig")
	if err != nil {
		t.Fatal(err)
	}
	params.Set("sig", signature)
	h := &lgPaymentwallHandler{config: lgPaymentwallConfig{secretKey: testLGSecret}}
	if h.verifyPingbackSignature(params) {
		t.Fatal("signature v2 accepted without explicit configuration")
	}
	h.config.allowSignatureV2 = true
	if !h.verifyPingbackSignature(params) {
		t.Fatal("signature v2 rejected after explicit configuration")
	}
	params.Set("sign_version", "1")
	if h.verifyPingbackSignature(params) {
		t.Fatal("deprecated signature v1 accepted")
	}
}

func TestPaymentwallStatusAPIUsesSignedExactQueryAndFixture(t *testing.T) {
	client := &http.Client{Transport: lgRoundTripper(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodGet || r.URL.Scheme != "https" || r.URL.Host != "api.paymentwall.com" || r.URL.Path != "/api/rest/payment/" {
			t.Fatalf("unexpected Paymentwall request: %s %s", r.Method, r.URL)
		}
		query := r.URL.Query()
		if query.Get("key") != "test_project_key" || query.Get("uid") != testLGUID || query.Get("ag_external_id") != lgPaymentwallProductSKU || query.Get("sign_version") != "3" {
			t.Fatalf("unexpected Status API query: %v", query)
		}
		if query.Get("sign") != "80a79ab3b4422c19f03156f94235f51290b494e02a8ae5c087badaf9016a5667" {
			t.Fatalf("unexpected Status API signature: %s", query.Get("sign"))
		}
		return lgJSONResponse(http.StatusOK, paymentwallApprovedFixture), nil
	})}
	status := &lgPaymentwallStatusAPI{
		projectKey: "test_project_key",
		secretKey:  testLGSecret,
		endpoint:   lgPaymentwallDefaultStatusURL,
		client:     client,
	}
	payments, err := status.queryByUID(context.Background(), testLGUID)
	if err != nil {
		t.Fatalf("queryByUID: %v", err)
	}
	if len(payments) != 1 || !paymentwallPaymentOwns(payments[0], testLGUID) {
		t.Fatalf("fixture did not validate as an owned payment: %#v", payments)
	}
}

func TestPaymentwallStatusAPIValidatesLegacyMediaType(t *testing.T) {
	tests := []struct {
		name      string
		mediaType string
		body      string
		want      int
	}{
		{"legacy payment history", "text/html", paymentwallApprovedFixture, 1},
		{"legacy empty history", "text/html; charset=UTF-8", "[]", 0},
		{"HTML error page", "text/html", "<html><body>Unavailable</body></html>", -1},
		{"legacy provider error", "text/html", `{"type":"Error","error":"User is invalid","code":4012}`, -1},
		{"unexpected media type", "text/plain", paymentwallApprovedFixture, -1},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			status := &lgPaymentwallStatusAPI{
				projectKey: "test_project_key",
				secretKey:  testLGSecret,
				endpoint:   lgPaymentwallDefaultStatusURL,
				client: &http.Client{Transport: lgRoundTripper(func(*http.Request) (*http.Response, error) {
					response := lgJSONResponse(http.StatusOK, tt.body)
					response.Header.Set("Content-Type", tt.mediaType)
					return response, nil
				})},
			}
			payments, err := status.queryByUID(context.Background(), testLGUID)
			if tt.want < 0 {
				if !errors.Is(err, errLGPaymentwallStatusUnavailable) {
					t.Fatalf("expected unavailable status, got %v", err)
				}
				return
			}
			if err != nil || len(payments) != tt.want {
				t.Fatalf("expected %d payments, got %d and error %v", tt.want, len(payments), err)
			}
		})
	}
}

func TestPaymentwallPaymentOwnershipRequiresEveryFixedField(t *testing.T) {
	var payments []lgPaymentwallPayment
	if err := json.Unmarshal([]byte(paymentwallApprovedFixture), &payments); err != nil {
		t.Fatal(err)
	}
	base := payments[0]
	if !paymentwallPaymentMatches(base, testLGUID, "ref123", lgPaymentwallProductSKU) {
		t.Fatal("approved fixture did not match")
	}
	tests := []struct {
		name   string
		change func(*lgPaymentwallPayment)
	}{
		{"wrong object", func(p *lgPaymentwallPayment) { p.Object = "subscription" }},
		{"wrong amount", func(p *lgPaymentwallPayment) { p.Amount = "2.98" }},
		{"wrong currency", func(p *lgPaymentwallPayment) { p.Currency = "CAD" }},
		{"wrong SKU", func(p *lgPaymentwallPayment) { p.ProductID = "other" }},
		{"wrong uid", func(p *lgPaymentwallPayment) { p.UID = strings.Repeat("f", 32) }},
		{"wrong reference", func(p *lgPaymentwallPayment) { p.ID = "other" }},
		{"not approved", func(p *lgPaymentwallPayment) { p.Risk = "pending" }},
		{"refunded", func(p *lgPaymentwallPayment) { refunded := true; p.Refunded = &refunded }},
		{"missing refund state", func(p *lgPaymentwallPayment) { p.Refunded = nil }},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			payment := base
			tc.change(&payment)
			if paymentwallPaymentMatches(payment, testLGUID, "ref123", lgPaymentwallProductSKU) {
				t.Fatal("invalid payment matched")
			}
		})
	}
}

func TestPaymentwallStatePersistsRecoveryCredentialDigestAndEnforcesSingleWriter(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	store, err := newLGPaymentwallStore(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := newLGPaymentwallStore(path); err == nil {
		t.Fatal("second process acquired the same state writer lock")
	}
	if err := store.registerIdentity(testLGUID, testLGCredential, time.Now()); err != nil {
		t.Fatal(err)
	}
	if !store.authorized(testLGUID, testLGCredential) || store.authorized(testLGUID, strings.Repeat("b", 64)) {
		t.Fatal("recovery credential authorization mismatch")
	}
	stateBytes, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(stateBytes, []byte(testLGCredential)) {
		t.Fatal("plaintext recovery credential was persisted")
	}
	digest := sha256.Sum256([]byte(testLGCredential))
	if !bytes.Contains(stateBytes, []byte(hex.EncodeToString(digest[:]))) {
		t.Fatal("credential digest was not persisted")
	}
	if err := store.close(); err != nil {
		t.Fatal(err)
	}

	restarted, err := newLGPaymentwallStore(path)
	if err != nil {
		t.Fatalf("reopen persisted state: %v", err)
	}
	defer restarted.close()
	if !restarted.authorized(testLGUID, testLGCredential) {
		t.Fatal("recovery credential did not survive restart")
	}
}

func TestPaymentwallStateCorruptionFailsClosed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	if err := os.WriteFile(path, []byte("{"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := newLGPaymentwallStore(path); !errors.Is(err, errLGPaymentwallInvalidState) {
		t.Fatalf("corrupt state error = %v", err)
	}
}

func TestPaymentwallPersistFailureDoesNotGrantOrMutateMemory(t *testing.T) {
	store := newLGTestStore(t)
	originalPath := store.path
	store.path = filepath.Dir(originalPath)
	err := store.registerIdentity(testLGUID, testLGCredential, time.Now())
	if err == nil {
		t.Fatal("expected persistence failure")
	}
	if store.authorized(testLGUID, testLGCredential) {
		t.Fatal("identity was retained in memory after persistence failure")
	}
	store.path = originalPath
}

func TestPaymentwallReconcilePreservesOtherApprovedPaymentAndRevokesLastRefund(t *testing.T) {
	store := newLGTestStore(t)
	now := time.Now().UTC()
	if err := store.registerIdentity(testLGUID, testLGCredential, now); err != nil {
		t.Fatal(err)
	}
	paymentA := approvedLGPayment("refA")
	paymentB := approvedLGPayment("refB")
	snapshot, err := store.reconcile(testLGUID, []lgPaymentwallPayment{paymentA, paymentB}, now.Add(-time.Second), now)
	if err != nil || !snapshot.Owned {
		t.Fatal("two approved purchases did not grant ownership")
	}
	if applied, err := store.applyNegativePingback(testLGUID, "refA", 2, now.Add(time.Second)); err != nil || !applied {
		t.Fatal("negative pingback did not persist")
	}
	paymentARefunded := paymentA
	paymentARefunded.Refunded = boolPointer(true)
	snapshot, err = store.reconcile(testLGUID, []lgPaymentwallPayment{paymentARefunded, paymentB}, now.Add(2*time.Second), now.Add(2*time.Second))
	if err != nil || !snapshot.Owned {
		t.Fatal("refund for one reference revoked a separate approved payment")
	}
	if reference := store.state.References["refA"]; !reference.Revoked || reference.Verified {
		t.Fatal("a refunded Status API result did not preserve the revocation")
	}
	if applied, err := store.applyNegativePingback(testLGUID, "refB", 220, now.Add(4*time.Second)); err != nil || !applied {
		t.Fatal("partial refund pingback did not persist")
	}
	paymentBRefunded := paymentB
	paymentBRefunded.Refunded = boolPointer(true)
	snapshot, err = store.reconcile(testLGUID, []lgPaymentwallPayment{paymentARefunded, paymentBRefunded}, now.Add(5*time.Second), now.Add(5*time.Second))
	if err != nil || snapshot.Owned {
		t.Fatal("refunded references restored access from stale approved results")
	}
	snapshot, err = store.reconcile(testLGUID, []lgPaymentwallPayment{paymentA, paymentBRefunded}, now.Add(6*time.Second), now.Add(6*time.Second))
	if err != nil || !snapshot.Owned {
		t.Fatal("fresh authoritative proof of refund reversal did not restore access")
	}
}

func TestPaymentwallNegativePingbackWinsAgainstOlderInFlightStatusProof(t *testing.T) {
	store := newLGTestStore(t)
	base := time.Now().UTC()
	if err := store.registerIdentity(testLGUID, testLGCredential, base); err != nil {
		t.Fatal(err)
	}
	if applied, err := store.applyNegativePingback(testLGUID, "refRace", 2, base.Add(time.Second)); err != nil || !applied {
		t.Fatal("negative pingback did not persist")
	}
	if applied, _ := store.applyVerifiedPositivePingback(testLGUID, "refRace", 0, base, base.Add(2*time.Second)); applied {
		t.Fatal("an older in-flight status response overruled a newer refund")
	}
	if applied, err := store.applyVerifiedPositivePingback(testLGUID, "refRace", 0, base.Add(3*time.Second), base.Add(4*time.Second)); err != nil || !applied {
		t.Fatal("new status proof after refund reversal was not accepted")
	}
}

func TestPaymentwallRepeatedRefundFenceWinsAgainstSameClockStatusResponse(t *testing.T) {
	store := newLGTestStore(t)
	base := time.Now().UTC()
	if err := store.registerIdentity(testLGUID, testLGCredential, base); err != nil {
		t.Fatal(err)
	}
	if applied, err := store.applyNegativePingback(testLGUID, "refRace", 2, base.Add(time.Second)); err != nil || !applied {
		t.Fatalf("initial refund: applied=%v err=%v", applied, err)
	}

	// Paymentwall later reverses the first refund. This query starts after that
	// callback and its authoritative approval restores the lifetime entitlement.
	reversalFence, err := store.captureMutationFence(testLGUID)
	if err != nil {
		t.Fatal(err)
	}
	reversalStarted := base.Add(2 * time.Second)
	snapshot, err := store.reconcileWithFence(testLGUID, []lgPaymentwallPayment{approvedLGPayment("refRace")}, reversalFence, reversalStarted, reversalStarted.Add(time.Second))
	if err != nil || !snapshot.Owned {
		t.Fatalf("fresh refund reversal: snapshot=%#v err=%v", snapshot, err)
	}

	// This older approval query remains in flight while a second refund callback
	// arrives. Its clock value intentionally equals the query start time.
	staleStarted := base.Add(4 * time.Second)
	staleFence, err := store.captureMutationFence(testLGUID)
	if err != nil {
		t.Fatal(err)
	}
	mutationBeforeRefund := store.state.References["refRace"].Mutation
	if applied, err := store.applyNegativePingback(testLGUID, "refRace", 2, staleStarted); err != nil || !applied {
		t.Fatalf("refund after reversal: applied=%v err=%v", applied, err)
	}
	mutationAfterRefund := store.state.References["refRace"].Mutation
	if mutationAfterRefund != mutationBeforeRefund+1 {
		t.Fatalf("duplicate refund did not advance its fence: before=%d after=%d", mutationBeforeRefund, mutationAfterRefund)
	}

	// A repeated negative while already revoked must also advance the persisted
	// fence, even though the ownership state and event key are unchanged.
	revisionBeforeRepeatedNegative := store.state.Identities[testLGUID].Revision
	if applied, err := store.applyNegativePingback(testLGUID, "refRace", 2, staleStarted); err != nil || !applied {
		t.Fatalf("repeated refund while revoked: applied=%v err=%v", applied, err)
	}
	if got := store.state.References["refRace"].Mutation; got != mutationAfterRefund+1 {
		t.Fatalf("negative callback while already revoked did not advance its fence: got=%d want=%d", got, mutationAfterRefund+1)
	}
	if got := store.state.Identities[testLGUID].Revision; got != revisionBeforeRepeatedNegative {
		t.Fatalf("repeated negative changed ownership revision without changing ownership: got=%d want=%d", got, revisionBeforeRepeatedNegative)
	}
	persistedState, err := os.ReadFile(store.path)
	if err != nil {
		t.Fatal(err)
	}
	var persisted lgPaymentwallState
	if err := json.Unmarshal(persistedState, &persisted); err != nil {
		t.Fatal(err)
	}
	if got := persisted.References["refRace"].Mutation; got != mutationAfterRefund+1 {
		t.Fatalf("negative mutation fence was not persisted: got=%d want=%d", got, mutationAfterRefund+1)
	}

	snapshot, err = store.reconcileWithFence(testLGUID, []lgPaymentwallPayment{approvedLGPayment("refRace")}, staleFence, staleStarted, staleStarted.Add(time.Second))
	if err != nil || snapshot.Owned || snapshot.Revision != 2 {
		t.Fatalf("same-clock stale approval restored a refunded purchase: snapshot=%#v err=%v", snapshot, err)
	}
	if reference := store.state.References["refRace"]; !reference.Revoked || reference.Verified || reference.Mutation != mutationAfterRefund+1 {
		t.Fatalf("stale approval changed the refund fence: reference=%#v", reference)
	}
}

func TestPaymentwallPositivePingbackWinsAgainstOlderEmptyStatusQuery(t *testing.T) {
	store := newLGTestStore(t)
	base := time.Now().UTC()
	if err := store.registerIdentity(testLGUID, testLGCredential, base); err != nil {
		t.Fatal(err)
	}
	if applied, err := store.applyVerifiedPositivePingback(testLGUID, "refRace", 0, base, base.Add(time.Second)); err != nil || !applied {
		t.Fatalf("fresh positive pingback: applied=%v err=%v", applied, err)
	}
	snapshot, err := store.reconcile(testLGUID, nil, base.Add(-time.Second), base.Add(2*time.Second))
	if err != nil || !snapshot.Owned || snapshot.Revision != 1 {
		t.Fatalf("older empty response revoked newer purchase: snapshot=%#v err=%v", snapshot, err)
	}
	if reference := store.state.References["refRace"]; !reference.Verified || reference.Revoked {
		t.Fatal("newer positive reference was overwritten by stale reconciliation")
	}
}

func TestPaymentwallStatusAPINullIsNotAuthoritativeEmptyHistory(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	store := newLGTestStore(t)
	now := time.Now().UTC()
	if err := store.registerIdentity(testLGUID, testLGCredential, now); err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.reconcile(testLGUID, []lgPaymentwallPayment{approvedLGPayment("ref123")}, now.Add(-time.Second), now)
	if err != nil || !snapshot.Owned {
		t.Fatalf("initial verified purchase: snapshot=%#v err=%v", snapshot, err)
	}
	h := testLGHandlerWithClient(t, store, privateKey, &http.Client{Transport: lgRoundTripper(func(*http.Request) (*http.Response, error) {
		return lgJSONResponse(http.StatusOK, "null"), nil
	})})
	request := httptest.NewRequest(http.MethodGet, lgPaymentwallEntitlementPath, nil)
	request.Header.Set("Authorization", "Bearer "+testLGCredential)
	request.Header.Set("X-Pleazyng-LG-UID", testLGUID)
	recorder := httptest.NewRecorder()
	h.serveEntitlement(recorder, request)
	if recorder.Code != http.StatusBadGateway || !store.state.Identities[testLGUID].Owned {
		t.Fatalf("null response status=%d owned=%v", recorder.Code, store.state.Identities[testLGUID].Owned)
	}
}

func TestPaymentwallPostRenameSyncFailurePoisonsStore(t *testing.T) {
	store := newLGTestStore(t)
	now := time.Now().UTC()
	if err := store.registerIdentity(testLGUID, testLGCredential, now); err != nil {
		t.Fatal(err)
	}
	store.syncDir = func(string) error { return errors.New("injected directory fsync failure") }
	if _, err := store.reconcile(testLGUID, []lgPaymentwallPayment{approvedLGPayment("ref123")}, now.Add(-time.Second), now); err == nil {
		t.Fatal("post-rename directory sync failure was ignored")
	}
	if store.healthy() || store.state.Identities[testLGUID].Owned {
		t.Fatal("store remained usable or granted in memory after failed directory sync")
	}
	if err := store.registerIdentity(testLGUID, testLGCredential, now.Add(time.Second)); !errors.Is(err, errLGPaymentwallStorePoisoned) {
		t.Fatalf("write after poison error = %v", err)
	}
	if _, err := os.Stat(store.path); err != nil {
		t.Fatalf("post-rename state file is missing: %v", err)
	}
}

func TestPaymentwallRestoreIssuesSignedDurableRevocationGrant(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	store := newLGTestStore(t)
	now := time.Now().UTC()
	if err := store.registerIdentity(testLGUID, testLGCredential, now); err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.reconcile(testLGUID, []lgPaymentwallPayment{approvedLGPayment("ref123")}, now.Add(-time.Second), now)
	if err != nil || !snapshot.Owned {
		t.Fatalf("initial verified purchase: owned=%v err=%v", snapshot.Owned, err)
	}
	refunded := approvedLGPayment("ref123")
	refunded.Refunded = boolPointer(true)
	h := testLGHandler(t, store, privateKey, []lgPaymentwallPayment{refunded})
	request := httptest.NewRequest(http.MethodGet, lgPaymentwallEntitlementPath, nil)
	request.Header.Set("Authorization", "Bearer "+testLGCredential)
	request.Header.Set("X-Pleazyng-LG-UID", testLGUID)
	recorder := httptest.NewRecorder()
	h.serveEntitlement(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("restore status = %d: %s", recorder.Code, recorder.Body.String())
	}
	var response lgPaymentwallEntitlementResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(response.Grant, ".")
	if len(parts) != 2 {
		t.Fatalf("invalid revocation grant envelope: %q", response.Grant)
	}
	body, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		t.Fatal(err)
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil || !ed25519.Verify(publicKey, body, signature) {
		t.Fatal("revocation grant signature did not verify")
	}
	var grant lgPaymentwallGrantPayload
	if err := json.Unmarshal(body, &grant); err != nil {
		t.Fatal(err)
	}
	if grant.Owned || grant.Revision != 2 || store.state.Identities[testLGUID].Owned {
		t.Fatalf("refund did not durably lower ownership: grant=%#v", grant)
	}
}

func TestPaymentwallRestoreDoesNotIssueStaleGrantWhenRefundPersistenceFails(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	store := newLGTestStore(t)
	now := time.Now().UTC()
	if err := store.registerIdentity(testLGUID, testLGCredential, now); err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.reconcile(testLGUID, []lgPaymentwallPayment{approvedLGPayment("ref123")}, now.Add(-time.Second), now)
	if err != nil || !snapshot.Owned {
		t.Fatalf("initial verified purchase: owned=%v err=%v", snapshot.Owned, err)
	}
	path := store.path
	store.path = filepath.Dir(path)
	h := testLGHandler(t, store, privateKey, []lgPaymentwallPayment{})
	request := httptest.NewRequest(http.MethodGet, lgPaymentwallEntitlementPath, nil)
	request.Header.Set("Authorization", "Bearer "+testLGCredential)
	request.Header.Set("X-Pleazyng-LG-UID", testLGUID)
	recorder := httptest.NewRecorder()
	h.serveEntitlement(recorder, request)
	if recorder.Code != http.StatusServiceUnavailable || store.state.Identities[testLGUID].Owned != true {
		t.Fatalf("failed revocation persistence response=%d owned=%v", recorder.Code, store.state.Identities[testLGUID].Owned)
	}
	if strings.HasPrefix(recorder.Header().Get("Content-Type"), "application/json") {
		t.Fatal("server emitted an ownership response after revocation persistence failed")
	}
	store.path = path
}

func TestPaymentwallEntitlementGrantIsServerSignedAndPersistsBeforeGrant(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	store := newLGTestStore(t)
	if err := store.registerIdentity(testLGUID, testLGCredential, time.Now()); err != nil {
		t.Fatal(err)
	}
	h := testLGHandler(t, store, privateKey, []lgPaymentwallPayment{approvedLGPayment("ref123")})
	request := httptest.NewRequest(http.MethodGet, lgPaymentwallEntitlementPath, nil)
	request.Header.Set("Authorization", "Bearer "+testLGCredential)
	request.Header.Set("X-Pleazyng-LG-UID", testLGUID)
	recorder := httptest.NewRecorder()
	h.serveEntitlement(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("entitlement status = %d: %s", recorder.Code, recorder.Body.String())
	}
	var response lgPaymentwallEntitlementResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(response.Grant, ".")
	if len(parts) != 2 {
		t.Fatalf("invalid grant envelope %q", response.Grant)
	}
	body, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		t.Fatal(err)
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil || !ed25519.Verify(publicKey, body, signature) {
		t.Fatal("ownership grant signature did not verify")
	}
	var grant lgPaymentwallGrantPayload
	if err := json.Unmarshal(body, &grant); err != nil {
		t.Fatal(err)
	}
	if !grant.Owned || grant.UID != testLGUID || grant.ProductID != lgPaymentwallProductSKU || grant.Currency != "USD" || grant.AmountCents != 299 || grant.Revision != 1 {
		t.Fatalf("unexpected ownership grant: %#v", grant)
	}

	// An identity with no prior verified ownership must receive no grant if the
	// payment reference cannot be persisted.
	failedStore := newLGTestStore(t)
	if err := failedStore.registerIdentity(testLGUID, testLGCredential, time.Now()); err != nil {
		t.Fatal(err)
	}
	failedPath := failedStore.path
	failedStore.path = filepath.Dir(failedPath)
	failedHandler := testLGHandler(t, failedStore, privateKey, []lgPaymentwallPayment{approvedLGPayment("ref123")})
	request = httptest.NewRequest(http.MethodGet, lgPaymentwallEntitlementPath, nil)
	request.Header.Set("Authorization", "Bearer "+testLGCredential)
	request.Header.Set("X-Pleazyng-LG-UID", testLGUID)
	recorder = httptest.NewRecorder()
	failedHandler.serveEntitlement(recorder, request)
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("failed persistence entitlement status = %d", recorder.Code)
	}
	if failedStore.state.Identities[testLGUID].Owned {
		t.Fatal("persistence failure changed in-memory ownership")
	}
	failedStore.path = failedPath
}

func TestPaymentwallPingbackRequiresFreshStatusAndRefundReplayStaysRevoked(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	store := newLGTestStore(t)
	if err := store.registerIdentity(testLGUID, testLGCredential, time.Now()); err != nil {
		t.Fatal(err)
	}
	currentPayment := approvedLGPayment("ref123")
	statusClient := &http.Client{Transport: lgRoundTripper(func(*http.Request) (*http.Response, error) {
		body, err := json.Marshal([]lgPaymentwallPayment{currentPayment})
		if err != nil {
			return nil, err
		}
		return lgJSONResponse(http.StatusOK, string(body)), nil
	})}
	h := testLGHandlerWithClient(t, store, privateKey, statusClient)
	base := time.Date(2026, 11, 16, 12, 0, 0, 0, time.UTC)
	currentTime := base
	h.now = func() time.Time { return currentTime }
	positive := paymentwallPingbackValues("ref123", 0)
	positive.Set("sig", mustPaymentwallSignature(t, positive, "3"))
	if code := callLGPaymentwallPingback(h, positive); code != http.StatusOK {
		t.Fatalf("positive pingback status = %d", code)
	}
	if !store.state.Identities[testLGUID].Owned {
		t.Fatal("confirmed positive pingback did not grant")
	}

	negative := paymentwallPingbackValues("ref123", 2)
	negative.Set("sig", mustPaymentwallSignature(t, negative, "3"))
	currentTime = base.Add(time.Minute)
	if code := callLGPaymentwallPingback(h, negative); code != http.StatusOK {
		t.Fatalf("negative pingback status = %d", code)
	}
	if store.state.Identities[testLGUID].Owned {
		t.Fatal("refund pingback did not revoke")
	}
	currentPayment.Refunded = boolPointer(true)
	positive.Set("sig", mustPaymentwallSignature(t, positive, "3"))
	currentTime = base.Add(2 * time.Minute)
	if code := callLGPaymentwallPingback(h, positive); code != http.StatusServiceUnavailable {
		t.Fatalf("positive callback with refunded Status API result status = %d", code)
	}
	if store.state.Identities[testLGUID].Owned {
		t.Fatal("positive pingback replay overruled the refunded Status API result")
	}
	currentPayment.Refunded = boolPointer(false)
	currentTime = base.Add(3 * time.Minute)
	if code := callLGPaymentwallPingback(h, positive); code != http.StatusOK {
		t.Fatalf("positive callback after authoritative refund reversal status = %d", code)
	}
	if !store.state.Identities[testLGUID].Owned {
		t.Fatal("fresh authoritative refund reversal did not restore the grant")
	}
}

func TestPaymentwallPendingAndTestPingbacksNeverGrantFromApprovedFixture(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	store := newLGTestStore(t)
	if err := store.registerIdentity(testLGUID, testLGCredential, time.Now()); err != nil {
		t.Fatal(err)
	}
	var statusCalls int
	statusClient := &http.Client{Transport: lgRoundTripper(func(*http.Request) (*http.Response, error) {
		statusCalls++
		return lgJSONResponse(http.StatusOK, paymentwallApprovedFixture), nil
	})}
	h := testLGHandlerWithClient(t, store, privateKey, statusClient)
	pending := paymentwallPingbackValues("pending123", 200)
	pending.Set("sig", mustPaymentwallSignature(t, pending, "3"))
	if code := callLGPaymentwallPingback(h, pending); code != http.StatusOK {
		t.Fatalf("pending pingback status = %d", code)
	}
	if store.state.Identities[testLGUID].Owned || statusCalls != 0 {
		t.Fatalf("pending pingback granted from approved fixture (owned=%v, status calls=%d)", store.state.Identities[testLGUID].Owned, statusCalls)
	}

	testEvent := paymentwallPingbackValues("test123", 0)
	testEvent.Set("is_test", "1")
	testEvent.Set("sig", mustPaymentwallSignature(t, testEvent, "3"))
	if code := callLGPaymentwallPingback(h, testEvent); code != http.StatusOK {
		t.Fatalf("signed test pingback status = %d", code)
	}
	if store.state.Identities[testLGUID].Owned || statusCalls != 0 {
		t.Fatalf("test pingback granted from approved fixture (owned=%v, status calls=%d)", store.state.Identities[testLGUID].Owned, statusCalls)
	}
	if _, exists := store.state.References["test123"]; exists {
		t.Fatal("test reference entered production ownership state")
	}
}

func TestPaymentwallTestOnlyModeAcknowledgesOnlySignedTestEventsWithoutLiveAPI(t *testing.T) {
	for _, key := range []string{
		"LG_PAYMENTWALL_ENABLED", "LG_PAYMENTWALL_TEST_PINGBACK_ENABLED",
		"LG_PAYMENTWALL_STATUS_API_ENABLED", "LG_PAYMENTWALL_PROJECT_KEY",
		"LG_PAYMENTWALL_SECRET_KEY", "LG_PAYMENTWALL_PUBLIC_BASE_URL",
		"LG_PAYMENTWALL_STATE_FILE", "LG_PAYMENTWALL_GRANT_PRIVATE_KEY",
		"LG_PAYMENTWALL_ALLOWED_ORIGINS", "LG_PAYMENTWALL_ALLOW_SIG_V2",
	} {
		t.Setenv(key, "")
	}
	t.Setenv("LG_PAYMENTWALL_ENABLED", "false")
	t.Setenv("LG_PAYMENTWALL_TEST_PINGBACK_ENABLED", "true")
	t.Setenv("LG_PAYMENTWALL_PROJECT_KEY", "test_project_key")
	t.Setenv("LG_PAYMENTWALL_SECRET_KEY", testLGSecret)
	h, err := newLGPaymentwallHandler(clientIPResolver{})
	if err != nil {
		t.Fatalf("construct test-only handler without live Status API: %v", err)
	}
	if h.config.enabled || !h.config.testPingbackEnabled || h.store != nil || h.statusAPI != nil {
		t.Fatalf("test-only handler has live payment components: %#v", h.config)
	}
	testEvent := paymentwallPingbackValues("test123", 0)
	testEvent.Set("is_test", "1")
	testEvent.Set("sig", mustPaymentwallSignature(t, testEvent, "3"))
	if code := callLGPaymentwallPingback(h, testEvent); code != http.StatusOK {
		t.Fatalf("signed test event status = %d", code)
	}
	liveEvent := paymentwallPingbackValues("live123", 0)
	liveEvent.Set("sig", mustPaymentwallSignature(t, liveEvent, "3"))
	if code := callLGPaymentwallPingback(h, liveEvent); code != http.StatusServiceUnavailable {
		t.Fatalf("test-only mode accepted a live callback: status=%d", code)
	}
	identity := httptest.NewRequest(http.MethodPost, lgPaymentwallIdentityPath, strings.NewReader(`{"uid":"`+testLGUID+`"}`))
	identity.Header.Set("Content-Type", "application/json")
	identity.Header.Set("Authorization", "Bearer "+testLGCredential)
	recorder := httptest.NewRecorder()
	h.serveIdentity(recorder, identity)
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("test-only mode served the identity API: status=%d", recorder.Code)
	}
}

func TestPaymentwallRestoreOnlyIdentityCannotRegisterUnknownUID(t *testing.T) {
	store := newLGTestStore(t)
	if err := store.registerIdentity(testLGUID, testLGCredential, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	h := newLGPaymentwallHandlerWithDependencies(
		lgPaymentwallConfig{enabled: true, allowedOrigins: map[string]struct{}{"https://billing.pleazyng.app": {}}},
		clientIPResolver{}, store, nil, &lgPaymentwallStatusAPI{},
	)
	request := func(uid, credential string, restoreOnly bool) *httptest.ResponseRecorder {
		t.Helper()
		body := `{"uid":"` + uid + `","restoreOnly":` + strconv.FormatBool(restoreOnly) + `}`
		req := httptest.NewRequest(http.MethodPost, lgPaymentwallIdentityPath, strings.NewReader(body))
		req.RemoteAddr = "192.0.2.3:1234"
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+credential)
		recorder := httptest.NewRecorder()
		h.serveIdentity(recorder, req)
		return recorder
	}

	if got := request(testLGUID, testLGCredential, true).Code; got != http.StatusNoContent {
		t.Fatalf("existing identity restore status = %d, want 204", got)
	}
	unknownUID := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	if got := request(unknownUID, testLGCredential, true).Code; got != http.StatusUnauthorized {
		t.Fatalf("unknown restore identity status = %d, want 401", got)
	}
	if store.identityExists(unknownUID) {
		t.Fatal("restore-only request created an identity for an unknown UID")
	}
	if got := request(testLGUID, strings.Repeat("b", 64), true).Code; got != http.StatusUnauthorized {
		t.Fatalf("mismatched restore identity status = %d, want 401", got)
	}
	newUID := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	if got := request(newUID, strings.Repeat("c", 64), false).Code; got != http.StatusNoContent {
		t.Fatalf("normal first-registration status = %d, want 204", got)
	}
	if !store.identityExists(newUID) {
		t.Fatal("normal first-registration did not create an identity")
	}
}

func TestPaymentwallBrowserOriginIsExact(t *testing.T) {
	allowed := map[string]struct{}{"https://billing.pleazyng.app": {}}
	request := httptest.NewRequest(http.MethodGet, "/", nil)
	request.Header.Set("Origin", "https://billing.pleazyng.app.evil.test")
	recorder := httptest.NewRecorder()
	if !rejectLGPaymentwallOrigin(recorder, request, allowed) || recorder.Code != http.StatusForbidden {
		t.Fatal("lookalike origin was accepted")
	}
	request = httptest.NewRequest(http.MethodGet, "/", nil)
	request.Header.Set("Origin", "https://billing.pleazyng.app")
	recorder = httptest.NewRecorder()
	if rejectLGPaymentwallOrigin(recorder, request, allowed) {
		t.Fatalf("configured origin was rejected: %d", recorder.Code)
	}
}

func TestPaymentwallCheckoutPageCSPSupportsOnlyTheSmartTVSDKNeeds(t *testing.T) {
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	store := newLGTestStore(t)
	if err := store.registerIdentity(testLGUID, testLGCredential, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	h := testLGHandler(t, store, privateKey, nil)
	session, err := store.createCheckoutSession(testLGUID, h.now())
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, lgPaymentwallCheckoutPage+session, nil)
	request.Header.Set("Origin", "https://billing.pleazyng.app")
	recorder := httptest.NewRecorder()
	h.serveCheckoutPage(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("checkout page status = %d: %s", recorder.Code, recorder.Body.String())
	}
	policy := recorder.Header().Get("Content-Security-Policy")
	for _, directive := range []string{
		"script-src 'nonce-",
		"'unsafe-eval' https://api.paymentwall.com",
		"style-src 'nonce-",
		"style-src-attr 'unsafe-inline'",
		"connect-src https://api.paymentwall.com https://*.paymentwall.com",
		"frame-ancestors 'none'",
	} {
		if !strings.Contains(policy, directive) {
			t.Errorf("checkout CSP is missing %q: %s", directive, policy)
		}
	}
	if strings.Contains(policy, "google-analytics") || strings.Contains(policy, "frame-src https:;") || strings.Contains(policy, "form-action https:;") {
		t.Fatalf("checkout CSP allows an unneeded external destination: %s", policy)
	}
	if !strings.Contains(recorder.Body.String(), "unlock Pleazyng on this TV") ||
		!strings.Contains(recorder.Body.String(), "After payment, return to Pleazyng and choose Restore") ||
		strings.Contains(recorder.Body.String(), "on this account") {
		t.Fatalf("checkout page user copy is not plain or TV-specific: %s", recorder.Body.String())
	}
	body := recorder.Body.String()
	callbackPosition := strings.Index(body, "window.PWSmartGatewayOnLoadedCallback = openPaymentForm")
	loaderPosition := strings.Index(body, "document.head.appendChild(sdk)")
	if callbackPosition < 0 || loaderPosition < callbackPosition ||
		!strings.Contains(body, "if (formOpened) return") || strings.Contains(body, "if (window.PWSmartGateway) openPaymentForm()") {
		t.Fatalf("checkout does not register the SDK readiness callback before loading exactly once: %s", body)
	}
}

func TestPaymentwallDisabledHandlerNeverServesCheckout(t *testing.T) {
	h := newLGPaymentwallHandlerWithDependencies(lgPaymentwallConfig{}, clientIPResolver{}, nil, nil, nil)
	mux := http.NewServeMux()
	registerLGPaymentwallRoutes(mux, h)
	request := httptest.NewRequest(http.MethodPost, lgPaymentwallCheckoutPath, nil)
	recorder := httptest.NewRecorder()
	mux.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("disabled checkout status = %d", recorder.Code)
	}
}

func TestPaymentwallOnlyMuxExposesNoGeneralRelayRoutes(t *testing.T) {
	h := newLGPaymentwallHandlerWithDependencies(lgPaymentwallConfig{}, clientIPResolver{}, nil, nil, nil)
	mux := newLGPaymentwallOnlyMux(h)
	for _, path := range []string{"/relay", "/logs", "/posters", "/auth/start", "/commerce/comic-link"} {
		recorder := httptest.NewRecorder()
		mux.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, path, nil))
		if recorder.Code != http.StatusNotFound {
			t.Fatalf("billing-only mux exposed %s with status %d", path, recorder.Code)
		}
	}
	recorder := httptest.NewRecorder()
	mux.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/health", nil))
	if recorder.Code != http.StatusOK || recorder.Body.String() != "ok" {
		t.Fatalf("billing health endpoint status=%d body=%q", recorder.Code, recorder.Body.String())
	}
}

func approvedLGPayment(ref string) lgPaymentwallPayment {
	return lgPaymentwallPayment{
		Object: "payment", ID: ref, UID: testLGUID, ProductID: lgPaymentwallProductSKU,
		Amount: "2.99", Currency: "USD", Refunded: boolPointer(false), Risk: "approved",
	}
}

func boolPointer(value bool) *bool { return &value }

func testLGHandler(t *testing.T, store *lgPaymentwallStore, privateKey ed25519.PrivateKey, payments []lgPaymentwallPayment) *lgPaymentwallHandler {
	t.Helper()
	responseBody, err := json.Marshal(payments)
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Transport: lgRoundTripper(func(*http.Request) (*http.Response, error) {
		return lgJSONResponse(http.StatusOK, string(responseBody)), nil
	})}
	return testLGHandlerWithClient(t, store, privateKey, client)
}

func testLGHandlerWithClient(t *testing.T, store *lgPaymentwallStore, privateKey ed25519.PrivateKey, client *http.Client) *lgPaymentwallHandler {
	t.Helper()
	publicURL, _ := url.Parse("https://billing.pleazyng.app")
	config := lgPaymentwallConfig{
		enabled: true, publicBaseURL: publicURL,
		allowedOrigins: map[string]struct{}{"https://billing.pleazyng.app": {}},
		projectKey:     "test_project_key", secretKey: testLGSecret,
		grantPrivateKey: privateKey,
	}
	status := &lgPaymentwallStatusAPI{projectKey: config.projectKey, secretKey: config.secretKey, endpoint: lgPaymentwallDefaultStatusURL, client: client}
	h := newLGPaymentwallHandlerWithDependencies(config, clientIPResolver{}, store, client, status)
	fixedNow := time.Now().UTC().Add(time.Minute)
	h.now = func() time.Time { return fixedNow }
	return h
}

func lgJSONResponse(status int, body string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header:     http.Header{"Content-Type": []string{"application/json; charset=utf-8"}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func paymentwallPingbackValues(ref string, eventType int) url.Values {
	return url.Values{
		"uid":          {testLGUID},
		"type":         {strconv.Itoa(eventType)},
		"ref":          {ref},
		"goodsid":      {lgPaymentwallProductSKU},
		"sign_version": {"3"},
	}
}

func mustPaymentwallSignature(t *testing.T, values url.Values, version string) string {
	t.Helper()
	signature, err := paymentwallSignature(values, testLGSecret, version, "sig")
	if err != nil {
		t.Fatal(err)
	}
	return signature
}

func callLGPaymentwallPingback(h *lgPaymentwallHandler, params url.Values) int {
	request := httptest.NewRequest(http.MethodGet, lgPaymentwallPingbackPath+"?"+params.Encode(), nil)
	request.RemoteAddr = "216.127.71.10:443"
	recorder := httptest.NewRecorder()
	h.servePingback(recorder, request)
	return recorder.Code
}
