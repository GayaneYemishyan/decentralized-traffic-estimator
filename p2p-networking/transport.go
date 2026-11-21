package main

import (
	"context"
	"sync"

	pubsub "github.com/libp2p/go-libp2p-pubsub"
	"github.com/libp2p/go-libp2p/core/peer"
)

type MessageHandler func(from peer.ID, data []byte)

type Transport struct {
	topic    *pubsub.Topic
	sub      *pubsub.Subscription
	self     peer.ID
	mu       sync.RWMutex
	handlers []MessageHandler
}

func NewTransport(ctx context.Context, ps *pubsub.PubSub, self peer.ID, topicName string) (*Transport, error) {
	topic, err := ps.Join(topicName)
	if err != nil {
		return nil, err
	}
	sub, err := topic.Subscribe()
	if err != nil {
		return nil, err
	}
	t := &Transport{topic: topic, sub: sub, self: self}
	go t.readLoop(ctx)
	return t, nil
}

// Publish sends opaque bytes to all peers on the topic.
func (t *Transport) Publish(ctx context.Context, data []byte) error {
	return t.topic.Publish(ctx, data)
}

// OnMessage registers a callback for incoming messages from other peers.
func (t *Transport) OnMessage(cb MessageHandler) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.handlers = append(t.handlers, cb)
}

func (t *Transport) readLoop(ctx context.Context) {
	for {
		msg, err := t.sub.Next(ctx)
		if err != nil {
			return // context cancelled or subscription closed
		}
		if msg.GetFrom() == t.self {
			continue // gossipsub echoes our own messages back
		}
		t.mu.RLock()
		hs := append([]MessageHandler(nil), t.handlers...)
		t.mu.RUnlock()
		for _, h := range hs {
			h(msg.GetFrom(), msg.Data)
		}
	}
}