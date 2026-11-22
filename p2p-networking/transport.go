package main

import (
	"context"
	"encoding/json"
	"errors"
	"sync"

	"log"

	pubsub "github.com/libp2p/go-libp2p-pubsub"
	"github.com/libp2p/go-libp2p/core/peer"
)

const maxPayloadBytes = 65536

type MessageHandler func(from peer.ID, data []byte)

type Transport struct {
	topic    *pubsub.Topic
	sub      *pubsub.Subscription
	self     peer.ID
	mu       sync.RWMutex
	handlers []MessageHandler
}

// validPayload is the only check Go does: size and well-formed JSON.
// Schema, version, digest and origin checks live in Python (serializer.parse_envelope).
func validPayload(data []byte) error {
	if len(data) < 1 || len(data) > maxPayloadBytes {
		return errors.New("payload size must be 1..65536 bytes")
	}
	if !json.Valid(data) {
		return errors.New("payload is not valid JSON")
	}
	return nil
}

func NewTransport(ctx context.Context, ps *pubsub.PubSub, self peer.ID, topicName string) (*Transport, error) {
	err := ps.RegisterTopicValidator(topicName, func(_ context.Context, _ peer.ID, msg *pubsub.Message) bool {
		err := validPayload(msg.Data)
		if err != nil {
			log.Printf("rejected invalid message from %s: %v", msg.GetFrom().ShortString(), err)
		}
		return err == nil
	})
	if err != nil {
		return nil, err
	}
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

// PublishRaw sends opaque bytes to all peers on the topic.
func (t *Transport) PublishRaw(ctx context.Context, data []byte) error {
	if err := validPayload(data); err != nil {
		return err
	}
	return t.topic.Publish(ctx, data)
}

// TopicPeers returns peers currently subscribed to our topic.
func (t *Transport) TopicPeers() []peer.ID {
	return t.topic.ListPeers()
}

// OnMessage registers a callback for incoming messages from other peers.
// Callbacks run inline in readLoop and must not block.
func (t *Transport) OnMessage(cb MessageHandler) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.handlers = append(t.handlers, cb)
}

func (t *Transport) readLoop(ctx context.Context) {
	for {
		msg, err := t.sub.Next(ctx)
		if err != nil {
			return // ctx cancelled or subscription closed
		}
		if msg.GetFrom() == t.self {
			continue
		}
		t.mu.RLock()
		hs := append([]MessageHandler(nil), t.handlers...)
		t.mu.RUnlock()
		for _, h := range hs {
			h(msg.GetFrom(), msg.Data)
		}
	}
}
func (t *Transport) Close() {
	t.sub.Cancel()
	t.topic.Close()
}
