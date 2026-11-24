package main

import (
	"context"
	"log"
	"time"

	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/libp2p/go-libp2p/p2p/discovery/mdns"
	"github.com/multiformats/go-multiaddr"
)

type mdnsNotifee struct {
	ctx context.Context
	h   host.Host
}

func (n *mdnsNotifee) HandlePeerFound(pi peer.AddrInfo) {
	if pi.ID == n.h.ID() {
		return
	}
	if err := n.h.Connect(n.ctx, pi); err != nil {
		log.Printf("mdns connect to %s failed: %v", pi.ID, err)
	}
}

func StartDiscovery(ctx context.Context, h host.Host, bootstrapAddr string) error {
	// Join/leave tracking
	h.Network().Notify(&network.NotifyBundle{
		ConnectedF:    func(_ network.Network, c network.Conn) { log.Printf("peer joined: %s", c.RemotePeer()) },
		DisconnectedF: func(_ network.Network, c network.Conn) { log.Printf("peer left: %s", c.RemotePeer()) },
	})

	// mDNS: works for local processes; often not inside Docker networks
	svc := mdns.NewMdnsService(h, "traffic-estimator", &mdnsNotifee{ctx: ctx, h: h})
	if err := svc.Start(); err != nil {
		return err
	}
	go func() { <-ctx.Done(); svc.Close() }()

	// Bootstrap peer (needed for Docker compose)
	if bootstrapAddr != "" {
		ma, err := multiaddr.NewMultiaddr(bootstrapAddr)
		if err != nil {
			return err // a malformed address is a config error, stay fatal
		}
		pi, err := peer.AddrInfoFromP2pAddr(ma)
		if err != nil {
			return err
		}
		go maintainBootstrap(ctx, h, *pi)
	}
	return nil
}

func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-ctx.Done():
		return false
	}
}

// maintainBootstrap dials the bootstrap peer with exponential backoff and
// re-dials whenever the connection drops.
func maintainBootstrap(ctx context.Context, h host.Host, pi peer.AddrInfo) {
	const minWait, maxWait = time.Second, 30 * time.Second
	wait := minWait
	for {
		if h.Network().Connectedness(pi.ID) == network.Connected {
			wait = minWait
			if !sleepCtx(ctx, 5*time.Second) {
				return
			}
			continue
		}
		cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		err := h.Connect(cctx, pi)
		cancel()
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			log.Printf("bootstrap connect failed (retry in %s): %v", wait, err)
			if !sleepCtx(ctx, wait) {
				return
			}
			wait = min(wait*2, maxWait)
			continue
		}
		wait = minWait
	}
}
