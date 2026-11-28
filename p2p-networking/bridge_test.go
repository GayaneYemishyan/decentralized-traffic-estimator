package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/libp2p/go-libp2p/core/peer"
)

type subResp struct {
	BootID     string      `json:"boot_id"`
	Messages   []bridgeMsg `json:"messages"`
	NextCursor uint64      `json:"next_cursor"`
	Dropped    uint64      `json:"dropped"`
}

// newTestBridge builds a Bridge without a libp2p host; enough for /subscribe tests.
func newTestBridge() *Bridge {
	return &Bridge{bootID: "test-boot", notify: make(chan struct{})}
}

func doSubscribe(b *Bridge, query string) (subResp, error) {
	var r subResp
	rec := httptest.NewRecorder()
	b.handleSubscribe(rec, httptest.NewRequest(http.MethodGet, "/subscribe?"+query, nil))
	if rec.Code != http.StatusOK {
		return r, fmt.Errorf("status %d: %s", rec.Code, rec.Body.String())
	}
	err := json.Unmarshal(rec.Body.Bytes(), &r)
	return r, err
}

func subscribe(t *testing.T, b *Bridge, query string) subResp {
	t.Helper()
	r, err := doSubscribe(b, query)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func pushN(b *Bridge, n int) {
	for i := 0; i < n; i++ {
		b.push(peer.ID("test-peer"), []byte(`{"i":1}`))
	}
}

func TestSubscribeRingOverflowReportsDropped(t *testing.T) {
	b := newTestBridge()
	pushN(b, 1100)

	r := subscribe(t, b, "cursor=1&max=1&timeout_ms=0")
	// last=1100, oldest=77, so cursors 1..76 were overwritten.
	if r.Dropped != 76 {
		t.Fatalf("dropped=%d, want 76", r.Dropped)
	}
	if len(r.Messages) != 1 || r.Messages[0].Cursor != 77 {
		t.Fatalf("first message %+v, want cursor 77", r.Messages)
	}
	if r.NextCursor != 78 {
		t.Fatalf("next_cursor=%d, want 78", r.NextCursor)
	}
}

func TestSubscribePagination(t *testing.T) {
	b := newTestBridge()
	pushN(b, 5)

	r := subscribe(t, b, "cursor=1&max=2&timeout_ms=0")
	if len(r.Messages) != 2 || r.NextCursor != 3 || r.Dropped != 0 {
		t.Fatalf("got %d msgs, next=%d, dropped=%d", len(r.Messages), r.NextCursor, r.Dropped)
	}
	r = subscribe(t, b, "cursor=3&max=100&timeout_ms=0")
	if len(r.Messages) != 3 || r.NextCursor != 6 {
		t.Fatalf("got %d msgs, next=%d", len(r.Messages), r.NextCursor)
	}
}

func TestSubscribeTimeoutWhenEmpty(t *testing.T) {
	b := newTestBridge()
	r := subscribe(t, b, "cursor=1&timeout_ms=50")
	if len(r.Messages) != 0 || r.NextCursor != 1 {
		t.Fatalf("got %d msgs, next=%d", len(r.Messages), r.NextCursor)
	}
	if r.BootID != "test-boot" {
		t.Fatalf("boot_id=%q", r.BootID)
	}
}

func TestSubscribeLongPollWakesOnPush(t *testing.T) {
	b := newTestBridge()
	type result struct {
		r   subResp
		err error
	}
	done := make(chan result, 1)
	go func() {
		r, err := doSubscribe(b, "cursor=1&timeout_ms=5000")
		done <- result{r, err}
	}()

	time.Sleep(100 * time.Millisecond)
	pushN(b, 1)

	select {
	case res := <-done:
		if res.err != nil {
			t.Fatal(res.err)
		}
		if len(res.r.Messages) != 1 || res.r.Messages[0].Cursor != 1 {
			t.Fatalf("got %+v", res.r.Messages)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("long-poll did not wake on push")
	}
}

func TestPublishValidation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	_, tr := newTestNode(t, ctx)
	b := &Bridge{tr: tr, bootID: "test-boot", notify: make(chan struct{})}

	b64 := func(raw []byte) string { return base64.StdEncoding.EncodeToString(raw) }
	wrap := func(data string) []byte { out, _ := json.Marshal(map[string]string{"data": data}); return out }

	tests := []struct {
		name   string
		method string
		body   []byte
		want   int
	}{
		{"bad base64", http.MethodPost, wrap("!!!"), http.StatusBadRequest},
		{"not json", http.MethodPost, wrap(b64([]byte("not json"))), http.StatusBadRequest},
		{"empty data", http.MethodPost, wrap(""), http.StatusBadRequest},
		{"garbage body", http.MethodPost, []byte("garbage"), http.StatusBadRequest},
		{"oversize payload", http.MethodPost, wrap(b64(jsonOfSize(maxPayloadBytes + 1))), http.StatusBadRequest},
		{"oversize body", http.MethodPost, wrap(strings.Repeat("a", maxPublishBody)), http.StatusBadRequest},
		{"GET not allowed", http.MethodGet, nil, http.StatusMethodNotAllowed},
		{"valid", http.MethodPost, wrap(b64([]byte(`{"ok":1}`))), http.StatusOK},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(tc.method, "/publish", bytes.NewReader(tc.body))
			b.handlePublish(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("status %d, want %d (%s)", rec.Code, tc.want, rec.Body.String())
			}
		})
	}
}
