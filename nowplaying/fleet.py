"""Homelab health, read straight out of Uptime Kuma's database.

Kuma has no unauthenticated status API unless a status page is published, and
/metrics needs an API key. Rather than store another credential, this reads
kuma.db read-only over the existing passwordless SSH to the Proxmox node:

    laptop -> ssh root@pveA4 -> pct exec 124 -> sqlite3 (read-only)

Polled infrequently and cached -- fleet health doesn't change second to second.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field

# Site-specific; override via environment rather than editing this file.
PVE_HOST = os.environ.get("NOWPLAYING_PVE_HOST", "root@192.168.1.9")
CTID = os.environ.get("NOWPLAYING_KUMA_CTID", "124")
DB = os.environ.get("NOWPLAYING_KUMA_DB",
                    "file:/opt/uptime-kuma/data/kuma.db?mode=ro")
POLL_INTERVAL = float(os.environ.get("NOWPLAYING_FLEET_POLL", "120"))
TIMEOUT = 20

# One round trip: counts by status, then the names of anything down.
# Deliberately contains NO quote characters -- it travels through ssh, then
# `pct exec`, then sqlite3, and every quote would need escaping at each layer.
# Row kind 1 = a count row, 2 = a down-monitor name. A monitor that others
# sit under (a Kuma group) only repeats their status -- one dead service
# would otherwise count twice, itself and its group -- so groups are left out.
_SQL = (
    "with last as (select monitor_id, max(time) mt from heartbeat group by monitor_id), "
    "cur as (select m.name nm, h.status st from monitor m "
    "join last l on l.monitor_id=m.id "
    "join heartbeat h on h.monitor_id=m.id and h.time=l.mt "
    "where m.active=1 "
    "and m.id not in (select parent from monitor where parent is not null)) "
    "select 1, st, count(*) from cur group by st "
    "union all select 2, nm, 0 from cur where st=0;"
)


@dataclass
class Fleet:
    up: int = 0
    down: int = 0
    pending: int = 0
    maintenance: int = 0
    down_names: list[str] = field(default_factory=list)
    checked_at: float = 0.0
    error: str = ""

    @property
    def total(self) -> int:
        return self.up + self.down + self.pending + self.maintenance

    @property
    def healthy(self) -> bool:
        return self.down == 0 and not self.error

    def summary(self) -> tuple[str, str]:
        """(headline, detail) for a two-line display."""
        if self.error:
            return ("fleet status unavailable", self.error[:60])
        if self.down:
            names = ", ".join(self.down_names[:4])
            if len(self.down_names) > 4:
                names += f" +{len(self.down_names) - 4} more"
            word = "service" if self.down == 1 else "services"
            return (f"{self.down} {word} down", names)
        extra = f" · {self.pending} pending" if self.pending else ""
        return (f"all {self.up} services up", f"homelab healthy{extra}")


_cache: Fleet | None = None


def poll(force: bool = False) -> Fleet:
    global _cache
    now = time.time()
    if _cache and not force and (now - _cache.checked_at) < POLL_INTERVAL:
        return _cache

    remote = f'pct exec {CTID} -- sqlite3 -separator "\t" "{DB}" "{_SQL}"'
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", PVE_HOST, remote]
    result = Fleet(checked_at=now)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.error = f"kuma unreachable ({type(exc).__name__})"
        _cache = result
        return result
    if proc.returncode != 0:
        result.error = (proc.stderr or "query failed").strip().splitlines()[-1][:80]
        _cache = result
        return result

    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        kind = parts[0]
        if kind == "1" and len(parts) >= 3:
            try:
                status, count = int(parts[1]), int(parts[2])
            except ValueError:
                continue
            if status == 1:
                result.up = count
            elif status == 0:
                result.down = count
            elif status == 2:
                result.pending = count
            elif status == 3:
                result.maintenance = count
        elif kind == "2":
            name = parts[1].strip()
            if name:
                result.down_names.append(name)

    if result.total == 0 and not result.error:
        result.error = "no monitors returned"
    _cache = result
    return result
