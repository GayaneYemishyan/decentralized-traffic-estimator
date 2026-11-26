package main

import (
	"bytes"
	"context"
	"testing"
	"time"

	"github.com/libp2p/go-libp2p"
	pubsub "github.com/libp2p/go-libp2p-pubsub"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/peer"
)

// jsonOfSize returns a valid JSON string literal of exactly n bytes.
func jsonOfSize(n int) []byte {
	return append(append([]byte{'"'}, bytes.Repeat([]byte("a"), n-2)...), '"')
}

func TestValidPayload(t *testing.T) {
	tests := []struct {
		name string
		data []byte
		ok   bool
	}{
		{"nil", nil, false},
		{"empty", []byte{}, false},
		{"valid object", []byte(`{"ok":1}`), true},
		{"not json", []byte("not json"), false},
		{"exactly max size", jsonOfSize(maxPayloadBytes), true},
		{"over max size", jsonOfSize(maxPayloadBytes + 1), false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := validPayload(tc.data)
			if (err == nil) != tc.ok {
				t.Fatalf("validPayload(%s) err=%v, want ok=%v", tc.name, err, tc.ok)
			}
		})
	}
}

func newTestNode(t *testing.T, ctx context.Context) (host.Host, *Transport) {
	t.Helper()
	h, err := libp2p.New(libp2p.ListenAddrStrings("/ip4/127.0.0.1/tcp/0"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { h.Close() })
	ps, err := pubsub.NewGossipSub(ctx, h)
	if err != nil {
		t.Fatal(err)
	}
	tr, err := NewTransport(ctx, ps, h.ID(), "test-topic")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(tr.Close)
	return h, tr
}

func TestPublishRawRejectsInvalid(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	_, tr := newTestNode(t, ctx)
	if err := tr.PublishRaw(ctx, []byte("not json")); err == nil {
		t.Fatal("expected error for non-JSON payload")
	}
	if err := tr.PublishRaw(ctx, nil); err == nil {
		t.Fatal("expected error for empty payload")
	}
}

func TestTwoNodesExchangeMessage(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	h1, tr1 := newTestNode(t, ctx)
	h2, tr2 := newTestNode(t, ctx)

	got := make(chan []byte, 1)
	var from peer.ID
	tr2.OnMessage(func(p peer.ID, data []byte) {
		from = p
		select {
		case got <- data:
		default:
		}
	})

	if err := h2.Connect(ctx, peer.AddrInfo{ID: h1.ID(), Addrs: h1.Addrs()}); err != nil {
		t.Fatal(err)
	}

	// Wait until node1 sees node2 on the topic.
	deadline := time.Now().Add(10 * time.Second)
	for len(tr1.TopicPeers()) == 0 {
		if time.Now().After(deadline) {
			t.Fatal("node2 never appeared in node1's topic peers")
		}
		time.Sleep(100 * time.Millisecond)
	}

	want := []byte(`{"node":"n1","congestion":0.7}`)
	if err := tr1.PublishRaw(ctx, want); err != nil {
		t.Fatal(err)
	}

	select {
	case data := <-got:
		if !bytes.Equal(data, want) {
			t.Fatalf("got %s, want %s", data, want)
		}
		if from != h1.ID() {
			t.Fatalf("from=%s, want %s", from, h1.ID())
		}
	case <-time.After(10 * time.Second):
		t.Fatal("timed out waiting for message on node2")
	}
}
