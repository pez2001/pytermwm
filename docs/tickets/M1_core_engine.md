# M1 - Core engine

| id | title | plan.md feature | acceptance | status |
|----|-------|-----------------|------------|--------|
| PTW-001 | Project skeleton, packaging, requirements.txt | requirements.txt, minimal dependencies | `pip install -r requirements.txt` only needs PyYAML; `python -m pytermwm --help` works | done |
| PTW-002 | ANSI/VT screen emulator | complete ansi support | CSI cursor/erase/scroll/insert/delete, SGR, scroll regions, alt screen, OSC titles, DEC line drawing, wide chars, DSR/DA replies | done |
| PTW-003 | Extended colors | more colors / background colors | 16/256/truecolor fg+bg, downgrade to client depth | done |
| PTW-004 | CP437 on-the-fly conversion | CP437 -> utf-8 | per-window toggle (yaml/hotkey), decoder swap without losing state | done |
| PTW-005 | Window model + sources | multiple windows (process/file/pipe; default own bash) | pty process, pipe process, file/fifo tail, fd, internal windows | done |
| PTW-006 | History buffer | window history buffer (configurable) | per-window `history` lines, scroll back/forward commands | done |
| PTW-007 | Virtual size + scrollbars | titles, optional scrollbars, virtual window size | virtual size fixed by user or auto-adapts; scrollbars auto/on/off | done |
| PTW-008 | Overflow handling | window text content overflow handling | wrap / clip / scroll modes | done |
| PTW-009 | Recursive tile layout | tile layout with recursive sub tiles | split any leaf any depth, resize ratios, swap, rotate | done |
| PTW-010 | Auto tiling | auto tiling | master-stack, spiral, columns, rows, monocle | done |
| PTW-011 | Grid + table layouts | grid, tables | auto grid, fixed cols; table with spans and col widths | done |
| PTW-012 | Float layout + floating windows | free float, floating windows | per-window rect, move/resize, z-order; float overlays on tiling | done |
| PTW-013 | Docking windows | docking windows | dock left/right/top/bottom with fixed size; rest goes to layout | done |
| PTW-014 | Multiple desktops | multiple desktops | own window sets, switch, move window, rename | done |
| PTW-015 | Compositor + diff renderer | (rendering) | frame compose, per-row diff output, cursor placement | done |
| PTW-016 | Keys, mouse, hotkeys | modern style hotkey driven navigation | key parser, direct + prefix keymaps, mouse focus/scroll | done |
| PTW-017 | Command registry | (foundation for remote/scripts) | named commands w/ args & help, shared by all frontends | done |
| PTW-018 | Main loop + tty handling | (foundation) | raw mode, resize, SIGCHLD, select loop | done |
| PTW-019 | Focus cues + titles | visible cues for focused windows, window titles | focus border color/glyph, titles from OSC/config | done |
| PTW-020 | Stdio rerouting | on the fly changing of stdin/out/err pipes between windows | `route` command + API | done |
