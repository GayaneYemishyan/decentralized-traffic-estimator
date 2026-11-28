package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"log"
	"net"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/peer"
)

const (
	ringSize        = 1024
	defaultPollMax  = 100
	defaultPollWait = 25 * time.Second
	maxPollWait     = 30 * time.Second
	// base64 of 64 KiB is ~87 KB; leave room for the JSON wrapper.
	maxPublishBody = 96 << 10
)

type bridgeMsg struct {
	Cursor uint64 `json:"cursor"`
	From   string `json:"from"`
	Data   string `json:"data"` // base64
}

type Bridge struct {
	h      host.Host
	tr     *Transport
	bootID string

	mu     sync.Mutex
	ring   [ringSize]bridgeMsg // message with cursor c lives at ring[(c-1)%ringSize]
	last   uint64              // cursor of newest message; 0 = none yet
	notify chan struct{}       // closed and replaced on every push
}

func newBootID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

// StartBridge listens synchronously (so a bad address fails fast), then serves in the background.
func StartBridge(ctx context.Context, h host.Host, tr *Transport, addr string) (*Bridge, error) {
	b := &Bridge{h: h, tr: tr, bootID: newBootID(), notify: make(chan struct{})}
	tr.OnMessage(b.push)

	mux := http.NewServeMux()
	mux.HandleFunc("/publish", b.handlePublish)
	mux.HandleFunc("/subscribe", b.handleSubscribe)
	mux.HandleFunc("/health", b.handleHealth)
	mux.HandleFunc("/peers", b.handlePeers)

	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, err
	}
	srv := &http.Server{Handler: mux}
	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			log.Printf("bridge: %v", err)
		}
	}()
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		srv.Shutdown(sctx)
	}()
	log.Printf("bridge listening on %s (boot_id=%s)", ln.Addr(), b.bootID)
	return b, nil
}

// push is called inline from Transport.readLoop: it must never block.
func (b *Bridge) push(from peer.ID, data []byte) {
	enc := base64.StdEncoding.EncodeToString(data)
	b.mu.Lock()
	b.last++
	b.ring[(b.last-1)%ringSize] = bridgeMsg{Cursor: b.last, From: from.String(), Data: enc}
	close(b.notify)
	b.notify = make(chan struct{})
	b.mu.Unlock()
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func (b *Bridge) handlePublish(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "POST only")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxPublishBody)
	var req struct {
		Data string `json:"data"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid request body")
		return
	}
	raw, err := base64.StdEncoding.DecodeString(req.Data)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "data is not valid base64")
		return
	}
	if err := validPayload(raw); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := b.tr.PublishRaw(r.Context(), raw); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (b *Bridge) handleSubscribe(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	cursor, _ := strconv.ParseUint(q.Get("cursor"), 10, 64)
	if cursor < 1 {
		cursor = 1
	}
	limit := defaultPollMax
	if v, err := strconv.Atoi(q.Get("max")); err == nil && v > 0 {
		limit = min(v, ringSize)
	}
	wait := defaultPollWait
	if v, err := strconv.Atoi(q.Get("timeout_ms")); err == nil && v >= 0 {
		wait = min(time.Duration(v)*time.Millisecond, maxPollWait)
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()

	var dropped uint64
	for {
		b.mu.Lock()
		oldest := uint64(1)
		if b.last > ringSize {
			oldest = b.last - ringSize + 1
		}
		if cursor < oldest { // client fell behind the ring: report the gap explicitly
			dropped += oldest - cursor
			cursor = oldest
		}
		if cursor <= b.last {
			msgs := make([]bridgeMsg, 0, limit)
			for c := cursor; c <= b.last && len(msgs) < limit; c++ {
				msgs = append(msgs, b.ring[(c-1)%ringSize])
			}
			next := cursor + uint64(len(msgs))
			b.mu.Unlock()
			writeJSON(w, http.StatusOK, b.subscribeResp(msgs, next, dropped))
			return
		}
		ch := b.notify
		b.mu.Unlock()

		select {
		case <-ch:
		case <-timer.C:
			writeJSON(w, http.StatusOK, b.subscribeResp([]bridgeMsg{}, cursor, dropped))
			return
		case <-r.Context().Done():
			return
		}
	}
}

func (b *Bridge) subscribeResp(msgs []bridgeMsg, next, dropped uint64) map[string]any {
	return map[string]any{
		"boot_id":     b.bootID,
		"messages":    msgs,
		"next_cursor": next,
		"dropped":     dropped,
	}
}

func (b *Bridge) handleHealth(w http.ResponseWriter, _ *http.Request) {
	topicPeers := b.tr.TopicPeers()
	writeJSON(w, http.StatusOK, map[string]any{
		"peer_id":     b.h.ID().String(),
		"peer_count":  len(b.h.Network().Peers()),
		"topic_peers": len(topicPeers),
		"boot_id":     b.bootID,
	})
}

func (b *Bridge) handlePeers(w http.ResponseWriter, _ *http.Request) {
	ids := []string{}
	for _, p := range b.h.Network().Peers() {
		ids = append(ids, p.String())
	}
	writeJSON(w, http.StatusOK, map[string]any{"peers": ids})
}
