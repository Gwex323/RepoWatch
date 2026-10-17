package bounds

import "testing"

func TestBelowLimit(t *testing.T) {
	if actual := Bound(2, 5); actual != 2 {
		t.Fatalf("got %d, want 2", actual)
	}
}
