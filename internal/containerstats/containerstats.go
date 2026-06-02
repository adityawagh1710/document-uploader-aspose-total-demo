// Package containerstats reads container resource stats from /sys/fs/cgroup
// and /proc, replacing the docker stats / docker top subprocess path.
//
// Ported from office_convert/container_stats.py. Works on cgroup v1 + v2.
package containerstats

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	cgroupV2Marker = "/sys/fs/cgroup/cgroup.controllers"
	cpuV2          = "/sys/fs/cgroup/cpu.stat"
	memCurrentV2   = "/sys/fs/cgroup/memory.current"
	memMaxV2       = "/sys/fs/cgroup/memory.max"
	pidsCurrentV2  = "/sys/fs/cgroup/pids.current"

	cpuacctV1     = "/sys/fs/cgroup/cpuacct/cpuacct.usage"
	memUsageV1    = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
	memLimitV1    = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
	pidsCurrentV1 = "/sys/fs/cgroup/pids/pids.current"
)

func isCgroupV2() bool {
	_, err := os.Stat(cgroupV2Marker)
	return err == nil
}

func readInt(path string, def int64) int64 {
	b, err := os.ReadFile(path)
	if err != nil {
		return def
	}
	v, err := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64)
	if err != nil {
		return def
	}
	return v
}

// ReadContainerStats returns a snapshot of CPU/memory/PID counters + timestamp.
// Mirrors read_container_stats.
func ReadContainerStats() map[string]any {
	v2 := isCgroupV2()
	var cpuUsageUsec, memBytes, memMaxBytes, pidsCurrent int64

	if v2 {
		if b, err := os.ReadFile(cpuV2); err == nil {
			for _, line := range strings.Split(string(b), "\n") {
				if strings.HasPrefix(line, "usage_usec") {
					parts := strings.Fields(line)
					if len(parts) >= 2 {
						cpuUsageUsec, _ = strconv.ParseInt(parts[1], 10, 64)
					}
					break
				}
			}
		}
		memBytes = readInt(memCurrentV2, 0)
		if b, err := os.ReadFile(memMaxV2); err == nil {
			raw := strings.TrimSpace(string(b))
			if raw != "" && raw != "max" {
				memMaxBytes, _ = strconv.ParseInt(raw, 10, 64)
			}
		}
		pidsCurrent = readInt(pidsCurrentV2, 0)
	} else {
		cpuUsageUsec = readInt(cpuacctV1, 0) / 1000 // ns -> us
		memBytes = readInt(memUsageV1, 0)
		memMaxBytes = readInt(memLimitV1, 0)
		if memMaxBytes >= 1<<62 {
			memMaxBytes = 0
		}
		pidsCurrent = readInt(pidsCurrentV1, 0)
	}

	version := 1
	if v2 {
		version = 2
	}
	return map[string]any{
		"cpu_usage_usec": cpuUsageUsec,
		"mem_bytes":      memBytes,
		"mem_max_bytes":  memMaxBytes,
		"pids_current":   pidsCurrent,
		"sampled_at":     float64(time.Now().UnixNano()) / 1e9,
		"cgroup_version": version,
	}
}

// sysconfClockTick returns SC_CLK_TCK. The kernel value is effectively always
// 100 on Linux (USER_HZ); avoiding a cgo/x-sys dependency for one constant.
func sysconfClockTick() int { return 100 }

func systemBootUptimeSec() float64 {
	b, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	fields := strings.Fields(string(b))
	if len(fields) == 0 {
		return 0
	}
	v, _ := strconv.ParseFloat(fields[0], 64)
	return v
}

// ListWorkers enumerates worker processes by walking /proc/[pid]. Filters to
// processes whose argv[0] basename starts with prefix. Mirrors list_workers.
func ListWorkers(prefix string) []map[string]any {
	if prefix == "" {
		prefix = "office-convert-worker"
	}
	workers := []map[string]any{}
	clockTicks := float64(sysconfClockTick())
	pageSize := int64(os.Getpagesize())
	bootUptime := systemBootUptimeSec()
	sampledAt := float64(time.Now().UnixNano()) / 1e9

	entries, err := os.ReadDir("/proc")
	if err != nil {
		return workers
	}
	for _, e := range entries {
		name := e.Name()
		if _, err := strconv.Atoi(name); err != nil {
			continue
		}
		cmdlineRaw, err := os.ReadFile(filepath.Join("/proc", name, "cmdline"))
		if err != nil || len(cmdlineRaw) == 0 {
			continue
		}
		cmdline := strings.TrimSpace(strings.ReplaceAll(string(cmdlineRaw), "\x00", " "))
		first := ""
		if fields := strings.Fields(cmdline); len(fields) > 0 {
			first = fields[0]
		}
		base := first
		if i := strings.LastIndex(base, "/"); i >= 0 {
			base = base[i+1:]
		}
		if !strings.HasPrefix(base, prefix) {
			continue
		}

		statText, err := os.ReadFile(filepath.Join("/proc", name, "stat"))
		if err != nil {
			continue
		}
		rparen := strings.LastIndex(string(statText), ")")
		if rparen < 0 {
			continue
		}
		after := strings.Fields(string(statText)[rparen+2:])
		if len(after) < 20 {
			continue
		}
		utime, e1 := strconv.ParseInt(after[11], 10, 64)
		stime, e2 := strconv.ParseInt(after[12], 10, 64)
		starttime, e3 := strconv.ParseInt(after[19], 10, 64)
		if e1 != nil || e2 != nil || e3 != nil {
			continue
		}
		cpuUsageUsec := int64(float64(utime+stime) * 1_000_000 / clockTicks)

		var rssBytes int64
		if statmRaw, err := os.ReadFile(filepath.Join("/proc", name, "statm")); err == nil {
			parts := strings.Fields(string(statmRaw))
			if len(parts) >= 2 {
				if rssPages, err := strconv.ParseInt(parts[1], 10, 64); err == nil {
					rssBytes = rssPages * pageSize
				}
			}
		}
		starttimeSec := float64(starttime) / clockTicks
		etimeSec := bootUptime - starttimeSec
		if etimeSec < 0 {
			etimeSec = 0
		}
		pid, _ := strconv.Atoi(name)
		workers = append(workers, map[string]any{
			"pid":            pid,
			"cmdline":        cmdline,
			"cpu_usage_usec": cpuUsageUsec,
			"rss_bytes":      rssBytes,
			"etime_sec":      etimeSec,
			"sampled_at":     sampledAt,
		})
	}
	// Sort by pid ascending.
	for i := 1; i < len(workers); i++ {
		for j := i; j > 0 && workers[j-1]["pid"].(int) > workers[j]["pid"].(int); j-- {
			workers[j-1], workers[j] = workers[j], workers[j-1]
		}
	}
	return workers
}
