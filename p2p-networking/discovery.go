package main

import (
	"context"
	"log"

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

	// Bootstrap peer (needed for Docker compose)
	if bootstrapAddr != "" {
		ma, err := multiaddr.NewMultiaddr(bootstrapAddr)
		if err != nil {
			return err
		}
		pi, err := peer.AddrInfoFromP2pAddr(ma)
		if err != nil {
			return err
		}
		if err := h.Connect(ctx, *pi); err != nil {
			return err
		}
	}
	return nil
}
