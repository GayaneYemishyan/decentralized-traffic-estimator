package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/libp2p/go-libp2p"
	pubsub "github.com/libp2p/go-libp2p-pubsub"
	"github.com/libp2p/go-libp2p/core/crypto"
	"github.com/libp2p/go-libp2p/core/peer"
)

func loadOrCreateKey(path string) (crypto.PrivKey, error) {
	data, err := os.ReadFile(path)
	if err == nil {
		return crypto.UnmarshalPrivateKey(data)
	}
	if !os.IsNotExist(err) {
		return nil, err
	}
	priv, _, err := crypto.GenerateEd25519Key(rand.Reader)
	if err != nil {
		return nil, err
	}
	data, err = crypto.MarshalPrivateKey(priv)
	if err != nil {
		return nil, err
	}
	return priv, os.WriteFile(path, data, 0600)
}

func main() {
	keyPath := flag.String("key", "", "identity key file (empty = random identity each run)")
	port := flag.Int("port", 4001, "TCP listen port")
	bootstrap := flag.String("bootstrap", "", "multiaddr of a bootstrap peer")
	topic := flag.String("topic", "traffic-state", "pub/sub topic")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	opts := []libp2p.Option{libp2p.ListenAddrStrings(fmt.Sprintf("/ip4/0.0.0.0/tcp/%d", *port))}
	if *keyPath != "" {
		priv, err := loadOrCreateKey(*keyPath)
		if err != nil {
			log.Fatal(err)
		}
		opts = append(opts, libp2p.Identity(priv))
	}
	h, err := libp2p.New(opts...)
	if err != nil {
		log.Fatal(err)
	}
	defer h.Close()

	for _, a := range h.Addrs() {
		fmt.Printf("listening: %s/p2p/%s\n", a, h.ID())
	}

	ps, err := pubsub.NewGossipSub(ctx, h)
	if err != nil {
		log.Fatal(err)
	}
	tr, err := NewTransport(ctx, ps, h.ID(), *topic)
	if err != nil {
		log.Fatal(err)
	}
	tr.OnMessage(func(from peer.ID, env Envelope) {
		fmt.Printf("[%s] node=%s ts=%d payload=%s\n", from.ShortString(), env.NodeID, env.Timestamp, env.Payload)
	})

	if err := StartDiscovery(ctx, h, *bootstrap); err != nil {
		log.Fatal(err)
	}

	// Temporary: publish each stdin line
	go func() {
		sc := bufio.NewScanner(os.Stdin)
		for sc.Scan() {
			payload, _ := json.Marshal(map[string]string{"text": sc.Text()})
			env := Envelope{
				Version:   EnvelopeVersion,
				NodeID:    h.ID().String(),
				Timestamp: time.Now().Unix(),
				Payload:   payload,
			}
			if err := tr.Publish(ctx, env); err != nil {
				log.Printf("publish: %v", err)
			}
		}
	}()

	<-ctx.Done()
}
