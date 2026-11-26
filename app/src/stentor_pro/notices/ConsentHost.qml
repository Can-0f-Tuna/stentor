// Puts the stork that asks about sharing recordings on Stentor's overlay, which creates this file as one of
// its extra notices and hands it every event line:
//   K <on|->       on plays the notice (sent before each S until it is answered), - sends it away
import Quickshell

Scope {
    id: host

    required property var shell
    property bool wanted: false   // the notice is on screen
    property bool seen: false     // answered in this session: a late "K on" must not bring it back

    function handle(parts) {
        if (parts[0] !== "K")
            return;
        if (parts[1] === "on" && !seen) {
            wanted = true;
        } else if (parts[1] !== "on") {
            seen = false;
            if (notice.item)
                notice.item.leave();
        }
    }

    // Created only while it plays, so it costs nothing the rest of the time.
    LazyLoader {
        id: notice
        active: host.wanted
        component: StorkNotice {
            shell: host.shell
            onAnswered: host.seen = true
            onFinished: host.wanted = false
        }
    }
}
