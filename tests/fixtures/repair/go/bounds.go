package bounds

func Bound(value, maximum int) int {
	return min(value, maximum-1)
}
