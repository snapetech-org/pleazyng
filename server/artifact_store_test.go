package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestArtifactIDLookupFailureReleasesStoreForRetry(t *testing.T) {
	parent := t.TempDir()
	dir := filepath.Join(parent, "logs")
	logs := newLogStore(dir)
	info, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm()&0077 != 0 {
		t.Fatalf("diagnostic directory permissions %o", info.Mode().Perm())
	}
	if err := os.Rename(dir, dir+".saved"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dir, []byte("not a directory"), 0600); err != nil {
		t.Fatal(err)
	}
	calls := 0
	generate := logs.generateID
	logs.generateID = func() string {
		calls++
		if calls > 1 {
			t.Fatal("retried ID generation after a store lookup error")
		}
		return generate()
	}
	if _, _, err := logs.store("", []byte("log"), time.Now()); err == nil {
		t.Fatal("accepted an unavailable store")
	}
	if err := os.Remove(dir); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(dir+".saved", dir); err != nil {
		t.Fatal(err)
	}
	logs.generateID = generate
	if _, _, err := logs.store("", []byte("retry"), time.Now()); err != nil {
		t.Fatal(err)
	}
}

func TestRelayReadsIndexedSizeAndSupportsRanges(t *testing.T) {
	logs := newLogStore(t.TempDir())
	posters := newPosterStore(t.TempDir(), 20*maxPosterSize, time.Hour)
	h := newStorageHarness(t, logs, posters)
	data := []byte("abcdef")
	id, _, err := logs.store("", data, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	_, pe, err := posters.store("", data, "image/png", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, url, path string
		handle          http.HandlerFunc
	}{
		{"log", "/logs/" + id, logs.filePath(id), h.srv.handleGetLogs},
		{"poster", "/posters/" + pe.Filename, posters.filePath(pe.Filename), h.srv.handleGetPosters},
	} {
		t.Run(tc.name, func(t *testing.T) {
			info, err := os.Stat(tc.path)
			if err != nil {
				t.Fatal(err)
			}
			if runtime.GOOS != "windows" && info.Mode().Perm()&0077 != 0 {
				t.Fatalf("published file permissions %o", info.Mode().Perm())
			}
			r := httptest.NewRequest(http.MethodGet, tc.url, nil)
			r.Header.Set("Range", "bytes=1-3")
			w := httptest.NewRecorder()
			tc.handle(w, r)
			if w.Code != http.StatusPartialContent || w.Body.String() != "bcd" {
				t.Fatalf("range response %d %q", w.Code, w.Body.String())
			}
			if err := os.WriteFile(tc.path, []byte("damaged"), 0600); err != nil {
				t.Fatal(err)
			}
			w = httptest.NewRecorder()
			tc.handle(w, httptest.NewRequest(http.MethodGet, tc.url, nil))
			if w.Code != http.StatusNotFound {
				t.Fatalf("mismatched stored size returned %d", w.Code)
			}
		})
	}
}

func TestRestoredPosterSizeLimitMatchesUploadLimit(t *testing.T) {
	dir := t.TempDir()
	for name, size := range map[string]int64{"a": 0, "b": maxPosterSize, "c": maxPosterSize + 1} {
		f, err := os.Create(filepath.Join(dir, strings.Repeat(name, posterIDLength)+".png"))
		if err != nil {
			t.Fatal(err)
		}
		if err := f.Truncate(size); err != nil {
			t.Fatal(err)
		}
		if err := f.Close(); err != nil {
			t.Fatal(err)
		}
	}
	posters := newPosterStore(dir, 20*maxPosterSize, time.Hour)
	if len(posters.entries) != 1 || posters.used != maxPosterSize {
		t.Fatalf("restored %d entries and %d bytes", len(posters.entries), posters.used)
	}
}
