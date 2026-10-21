package main

import (
	"regexp"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/influxdata/influxql"
)

// Grafana 매크로를 실제 InfluxQL 로 치환한 뒤 파싱
func expand(q string) string {
	r := strings.NewReplacer(
		"$timeFilter", "time >= now() - 24h",
		"$__interval", "5m",
		"/^$host$/", "/^(mirror01|mirror02)$/",
		"/^$device$/", "/^(sda|nvme0)$/",
		"/^$iodev$/", "/^.*$/",
	)
	return r.Replace(q)
}

var macroRe = regexp.MustCompile(`\$[A-Za-z_{]`)

func main() {
	var qs []string
	if err := json.NewDecoder(os.Stdin).Decode(&qs); err != nil {
		panic(err)
	}
	bad := 0
	for _, q := range qs {
		e := expand(q)
		if macroRe.MatchString(e) {
			fmt.Println("UNEXPANDED:", e)
			bad++
			continue
		}
		stmt, err := influxql.ParseStatement(e)
		if err != nil {
			fmt.Printf("FAIL: %v\n  %s\n", err, e)
			bad++
			continue
		}
		_ = stmt
	}
	fmt.Printf("%d queries, %d bad\n", len(qs), bad)
	if bad > 0 {
		os.Exit(1)
	}
}
