package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/libp2p/go-libp2p"
	pubsub "github.com/libp2p/go-libp2p-pubsub"
	"github.com/libp2p/go-libp2p/core/peer"
)

func main() {
	port := flag.Int("port", 4001, "TCP listen port")
	bootstrap := flag.String("bootstrap", "", "multiaddr of a bootstrap peer")
	topic := flag.String("topic", "traffic-state", "pub/sub topic")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	h, err := libp2p.New(libp2p.ListenAddrStrings(fmt.Sprintf("/ip4/0.0.0.0/tcp/%d", *port)))
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
	tr.OnMessage(func(from peer.ID, data []byte) {
		fmt.Printf("[%s] %s\n", from.ShortString(), data)
	})

	if err := StartDiscovery(ctx, h, *bootstrap); err != nil {
		log.Fatal(err)
	}

	// Temporary: publish each stdin line
	go func() {
		sc := bufio.NewScanner(os.Stdin)
		for sc.Scan() {
			if err := tr.Publish(ctx, sc.Bytes()); err != nil {
				log.Printf("publish: %v", err)
			}
		}
	}()

	<-ctx.Done()
}
