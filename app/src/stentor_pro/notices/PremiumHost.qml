// Puts the "Stentor Cloud is on" notice on Stentor's overlay, which creates this file as one of its extra
// notices and hands it every event line:
//   P <on|->       on plays the notice (sent before each S until it is clicked), - sends it away
import Quickshell

Scope {
    id: host

    required property var shell
    property bool wanted: false   // the notice is on screen
    property bool seen: false     // clicked in this session: a late "P on" must not bring it back

    function handle(parts) {
        if (parts[0] !== "P")
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
        component: PremiumNotice {
            shell: host.shell
            onClicked: host.seen = true
            onFinished: host.wanted = false
        }
    }
}
