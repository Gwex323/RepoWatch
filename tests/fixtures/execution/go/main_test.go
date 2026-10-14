package main

import (
	"os"
	"testing"
)

func TestIsolation(t *testing.T) {
	if os.Getuid() != 1000 || os.Getenv("GITHUB_TOKEN") != "" {
		t.Fatal("unexpected identity or credentials")
	}
	if _, err := os.Stat("/var/run/docker.sock"); !os.IsNotExist(err) {
		t.Fatal("unexpected Docker socket")
	}
}
