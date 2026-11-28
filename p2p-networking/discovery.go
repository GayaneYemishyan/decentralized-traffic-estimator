package main

import (
	"context"
	"log"
	"sync"
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
	// Only the peer with the smaller ID dials, so both sides don't dial each other simultaneously.
	if n.h.ID() >= pi.ID || n.h.Network().Connectedness(pi.ID) == network.Connected {
		return
	}
	go func() {
		cctx, cancel := context.WithTimeout(n.ctx, 10*time.Second)
		defer cancel()
		if err := n.h.Connect(cctx, pi); err != nil {
			log.Printf("mdns connect to %s failed: %v", pi.ID, err)
		}
	}()
}

func StartDiscovery(ctx context.Context, h host.Host, bootstrapAddr string) error {
	// Join/leave tracking: exactly one line per join and one per leave
	var mu sync.Mutex
	up := map[peer.ID]bool{}
	h.Network().Notify(&network.NotifyBundle{
		ConnectedF: func(_ network.Network, c network.Conn) {
			p := c.RemotePeer()
			mu.Lock()
			defer mu.Unlock()
			if !up[p] {
				up[p] = true
				log.Printf("peer joined: %s", p)
			}
		},
		DisconnectedF: func(n network.Network, c network.Conn) {
			p := c.RemotePeer()
			if n.Connectedness(p) == network.Connected {
				return
			}
			mu.Lock()
			defer mu.Unlock()
			if up[p] {
				delete(up, p)
				log.Printf("peer left: %s", p)
			}
		},
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
