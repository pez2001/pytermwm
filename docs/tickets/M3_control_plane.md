# M3 - Control plane

| id | title | plan.md feature | acceptance | status |
|----|-------|-----------------|------------|--------|
| PTW-037 | Session server | sessions: keep running, detach, reattach | unix socket server, multi client attach/detach | done |
| PTW-038 | Session save/restore | sessions saving, restoring | snapshot json, restore recreates layout | done |
| PTW-039 | CLI tool | remotely controllable via python cli | `pytermwm ctl/ls/new/send/...` | done |
| PTW-040 | HTTP API | web service api | REST+SSE, token auth | done |
| PTW-041 | MCP integration | mcp protocol integration | stdio + http JSON-RPC tools/resources | done |
| PTW-042 | Scripting engine | scriptable (fullscreen on output etc.) | yaml rules + python hooks | done |
| PTW-043 | Plugin system | extendable via modules/plugins | builtin + user plugins, hot reload | done |
| PTW-044 | Internal pv | easy pipe viewer, internal pv wired to system | `pytermwm pv` reports progress to status line | done |
| PTW-045 | Stdin/web viewer window | stdin/web input viewer window | feed via pipe/HTTP, filter, pause, follow | done |
| PTW-046 | Debugger + console | debugger + console | python console window, event tracer, inspector | done |
| PTW-047 | Status viewer window | status viewer window | shows all status items | done |
| PTW-048 | Directory watcher window | directory watcher window | poll-based change list | done |
| PTW-049 | Charts | modern charts and diagrams | sparkline, bar, line(braille), gauge, chart window | done |
