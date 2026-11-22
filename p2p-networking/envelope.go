package main

import (
	"encoding/json"
	"errors"
	"fmt"
)

const EnvelopeVersion = 1

// Envelope mirrors envelope_schema.json (placeholder fields until schema is available).
type Envelope struct {
	Version   int             `json:"version"`
	NodeID    string          `json:"node_id"`
	Timestamp int64           `json:"timestamp"`
	Payload   json.RawMessage `json:"payload"`
}

func (e *Envelope) Validate() error {
	if e.Version != EnvelopeVersion {
		return fmt.Errorf("unsupported version %d", e.Version)
	}
	if e.NodeID == "" {
		return errors.New("missing node_id")
	}
	if e.Timestamp <= 0 {
		return errors.New("missing or invalid timestamp")
	}
	if len(e.Payload) == 0 || !json.Valid(e.Payload) {
		return errors.New("missing or invalid payload")
	}
	return nil
}

func EncodeEnvelope(e Envelope) ([]byte, error) {
	if err := e.Validate(); err != nil {
		return nil, err
	}
	return json.Marshal(e)
}

func DecodeEnvelope(data []byte) (Envelope, error) {
	var e Envelope
	if err := json.Unmarshal(data, &e); err != nil {
		return e, err
	}
	return e, e.Validate()
}
