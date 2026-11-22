#!/usr/bin/python3
"""A stand-in for the desktop's side of the GlobalShortcuts portal (org.freedesktop.impl.portal.GlobalShortcuts), so
the hotkey client can be checked against the real xdg-desktop-portal with no GNOME or KDE session: the portal does
the requests, sessions, app identity and signal routing, and this answers as a desktop would.

Run by tests/test_portal_wire.py with the system Python (it needs python3-gi, not this project's packages):

    portal_stand_in.py grant|other|unassigned|refuse|broken

- grant: every shortcut gets the key it asked for;  other: the first one gets Ctrl+D instead;
- unassigned: the first one gets no key;  refuse: the request is declined, as from a cancelled dialog;
- broken: the request fails, as with a portal installed for a desktop that is not the one running.

It prints what it was asked (`session APP_ID`, `bound ID=TRIGGER ...`, `closed`) and takes commands on stdin:
`press ID`, `release ID`, `change ID TRIGGER`, `close`.
"""
import os
import sys
import time

from gi.repository import Gio, GLib

NAME = "org.freedesktop.impl.portal.desktop.standin"
PATH = "/org/freedesktop/portal/desktop"
SHORTCUTS = "org.freedesktop.impl.portal.GlobalShortcuts"
SESSION = "org.freedesktop.impl.portal.Session"
XML = """<node>
  <interface name="org.freedesktop.impl.portal.GlobalShortcuts">
    <method name="CreateSession">
      <arg type="o" name="handle" direction="in"/><arg type="o" name="session_handle" direction="in"/>
      <arg type="s" name="app_id" direction="in"/><arg type="a{sv}" name="options" direction="in"/>
      <arg type="u" name="response" direction="out"/><arg type="a{sv}" name="results" direction="out"/>
    </method>
    <method name="BindShortcuts">
      <arg type="o" name="handle" direction="in"/><arg type="o" name="session_handle" direction="in"/>
      <arg type="a(sa{sv})" name="shortcuts" direction="in"/><arg type="s" name="parent_window" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="u" name="response" direction="out"/><arg type="a{sv}" name="results" direction="out"/>
    </method>
    <method name="ListShortcuts">
      <arg type="o" name="handle" direction="in"/><arg type="o" name="session_handle" direction="in"/>
      <arg type="u" name="response" direction="out"/><arg type="a{sv}" name="results" direction="out"/>
    </method>
    <signal name="Activated">
      <arg type="o" name="session_handle"/><arg type="s" name="shortcut_id"/><arg type="t" name="timestamp"/>
      <arg type="a{sv}" name="options"/>
    </signal>
    <signal name="Deactivated">
      <arg type="o" name="session_handle"/><arg type="s" name="shortcut_id"/><arg type="t" name="timestamp"/>
      <arg type="a{sv}" name="options"/>
    </signal>
    <signal name="ShortcutsChanged">
      <arg type="o" name="session_handle"/><arg type="a(sa{sv})" name="shortcuts"/>
    </signal>
    <property name="version" type="u" access="read"/>
  </interface>
  <interface name="org.freedesktop.impl.portal.Session">
    <method name="Close"/>
    <signal name="Closed"/>
    <property name="version" type="u" access="read"/>
  </interface>
</node>"""

policy = sys.argv[1] if len(sys.argv) > 1 else "grant"
node = Gio.DBusNodeInfo.new_for_xml(XML)
loop = GLib.MainLoop()
state = {"session": "", "shortcuts": {}, "registered": 0}


def say(*words):
    print(*words, flush=True)


def rows():
    return [(ident, {"description": GLib.Variant("s", label), "trigger_description": GLib.Variant("s", trigger)})
            for ident, (label, trigger) in state["shortcuts"].items()]


def listed():
    return GLib.Variant("a(sa{sv})", rows())


def on_session(bus, sender, path, interface, method, args, call):
    say("closed")
    call.return_value(None)


def on_shortcuts(bus, sender, path, interface, method, args, call):
    if method == "CreateSession":
        _, session, app_id, _ = args.unpack()
        state["session"] = session
        state["registered"] = bus.register_object(session, node.lookup_interface(SESSION), on_session, version, None)
        say("session", app_id or "-")
        call.return_value(GLib.Variant("(ua{sv})", (0, {})))
    elif method == "BindShortcuts":
        _, session, shortcuts, _, _ = args.unpack()
        if policy in ("refuse", "broken"):
            say("refused" if policy == "refuse" else "failed")
            call.return_value(GLib.Variant("(ua{sv})", (1 if policy == "refuse" else 2, {})))
            return
        state["shortcuts"] = {}
        for number, (ident, options) in enumerate(shortcuts):
            trigger = options.get("preferred_trigger", "")
            if number == 0 and policy == "other":
                trigger = "Ctrl+D"
            if number == 0 and policy == "unassigned":
                trigger = ""
            state["shortcuts"][ident] = (options.get("description", ""), trigger)
        say("bound", *(f"{ident}={trigger}" for ident, (_, trigger) in state["shortcuts"].items()))
        call.return_value(GLib.Variant("(ua{sv})", (0, {"shortcuts": listed()})))
    else:
        call.return_value(GLib.Variant("(ua{sv})", (0, {"shortcuts": listed()})))


def version(bus, sender, path, interface, name):
    return GLib.Variant("u", 1)


def commands(source, condition):
    data = os.read(source.fileno(), 65536)  # everything that came: a buffered readline would keep the second line back
    if not data:
        loop.quit()
        return False
    for line in data.decode().splitlines():
        command(line.split())
    return True


def command(words):
    bus = state["bus"]
    if words[0] in ("press", "release"):
        bus.emit_signal(None, PATH, SHORTCUTS, "Activated" if words[0] == "press" else "Deactivated",
                        GLib.Variant("(osta{sv})", (state["session"], words[1], int(time.monotonic() * 1000), {})))
    elif words[0] == "change":
        label, _ = state["shortcuts"][words[1]]
        state["shortcuts"][words[1]] = (label, " ".join(words[2:]))
        bus.emit_signal(None, PATH, SHORTCUTS, "ShortcutsChanged", GLib.Variant("(oa(sa{sv}))", (state["session"], rows())))
    elif words[0] == "close":
        bus.emit_signal(None, state["session"], SESSION, "Closed", None)


def on_bus(bus, name):
    state["bus"] = bus
    bus.register_object(PATH, node.lookup_interface(SHORTCUTS), on_shortcuts, version, None)


Gio.bus_own_name(Gio.BusType.SESSION, NAME, Gio.BusNameOwnerFlags.NONE, on_bus, lambda bus, name: say("ready"),
                 lambda bus, name: (say("no name"), loop.quit()))
GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, commands)
loop.run()
