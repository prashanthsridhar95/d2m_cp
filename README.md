# D2M Control Panel

A local dashboard for the D2M backend and frontend dev servers — status,
Start/Stop/Restart, logs, and a few one-click actions — instead of juggling
two terminal windows.

## Setup (one time)

This folder syncs through Google Drive, which doesn't always preserve the
"double-clickable" permission on `.command` files. **Move this whole
"Control Panel" folder to your Desktop first** (drag it in Finder) —
everything will still work correctly since it finds `d2m_core_engine` and
`d2m_web` via `~/Desktop/...` regardless of where the panel itself lives,
but a plain local folder is more reliable for double-clicking.

If double-clicking `D2M Control Panel.command` doesn't do anything, open
Terminal and run once:

```bash
chmod +x "~/Desktop/Control Panel/D2M Control Panel.command"
```

## Running it

Double-click **`D2M Control Panel.command`**. It opens `http://127.0.0.1:8765`
in your browser automatically — that page *is* the control panel.

If you'd rather run it from Terminal:

```bash
cd "~/Desktop/Control Panel"
python3 control_panel.py
```

(Same `python3` you already got working for `d2m_core_engine`.)

## What it does

Two service cards — **Backend API** (`:8000`) and **Frontend** (`:5173`) —
each with a status dot (green = healthy, yellow = starting, gray =
stopped, blue = something's running there that this panel didn't start)
and Start / Restart / Stop buttons. Status is based on an actual health
check against the running server, not just "is a process alive," so it's
accurate even if you started something manually in a separate terminal —
the panel will detect it and let you stop it too.

Quick actions: install backend/frontend dependencies, seed demo data
(`seed.py`), and run the backend test suite — each streams its real output
into a log panel so you can see exactly what happened, not just
pass/fail.

Stop/Restart kill the *entire* process tree for anything the panel itself
started (uvicorn's `--reload` and Vite's dev server both spawn child
processes — killing just the parent would leave the port stuck). For
processes it didn't start itself, it's deliberately more conservative and
only kills that exact process, never its whole process group, so it can't
accidentally take down an unrelated terminal session.

## Notes

- Leaving the control panel itself running costs nothing extra — it's a
  small local server on port 8765, and closing its Terminal window (if you
  launched it that way) does *not* stop backend/frontend processes it
  started; only the Stop buttons (or closing their own terminals, if
  started manually) do that.
- If port 8765 is already in use, the launcher just opens the existing
  panel instead of starting a second one.
- Logs it captures live at `~/Library/Application Support/D2MControlPanel/logs/`
  if you want to look at them outside the dashboard.
