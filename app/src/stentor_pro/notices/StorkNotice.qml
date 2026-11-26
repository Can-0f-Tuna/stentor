// The question about sharing recordings, asked by a stork. During a dictation it flies in, lands on the pill and
// walks along it, clatters its bill, plucks a bar that becomes an envelope, and carries it swinging to a card
// above the pill. Yes: it clatters, takes the envelope and flies off. No: it nods and leaves without it. No answer
// for 30 seconds: it leaves the same quiet way, and the next dictation brings it back. If the pill closes before
// the pluck, or sits at the top of the screen, it goes straight to the card, envelope in its bill.
// One clock, `t`, runs through keyframes copied from the approved draft, in its milliseconds, and sets the
// transforms of plain items: nothing is repainted. While it waits on the card the clock is stopped and only a
// blink runs. The overlay has no reduced-motion setting to follow.
import QtQuick
import QtQuick.Shapes
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

Scope {
    id: notice

    required property var shell     // overlay.qml: the pill's look and position
    signal answered()
    signal finished()

    readonly property real s: shell.sizeScale
    // Where the draft's pill sits on this screen. The draft's scene is scaled by s around it.
    readonly property real pillX: win.width / 2
    readonly property real pillY: shell.atTop ? 48 + 40 * s : win.height - 48 - 40 * s
    readonly property real pillTop: pillY - 28 * s
    readonly property real walkX: pillX + 102 * s          // where it lands on the pill
    readonly property real stepX: 8 * s
    readonly property real cardW: 228 * s
    readonly property real cardH: 196 * s
    readonly property real cardX: Math.min(pillX + 88 * s, win.width - cardW - 12)
    readonly property real cardY: shell.atTop ? pillY + 40 * s : pillTop - 12 * s - cardH
    readonly property real perchX: cardX + 112 * s
    readonly property real perchY: cardY
    readonly property int waitAt: 4900                     // the clock stops here until the answer
    readonly property int answerAt: 6920                   // and goes on from here
    readonly property int quietAt: 7120                    // or from here, with no click to show

    // "" before it starts | playing | waiting (for the click) | leaving
    property string phase: ""
    property real t: 300
    property bool yes: false
    property bool quiet: false                             // left unanswered: no button, no words
    property int tries: 0
    property int resumeAt: answerAt
    property var k: ({})                                   // the keyframes

    // Worked out when it starts: the animations must not follow what they move
    property real inX: 0
    property real inY: 0
    property real outX: 0
    property real outY: 0
    property real fromX: 0                                 // where it leaves for the card
    property real fromY: 0
    property real arriveMs: 920
    property real leaveMs: 900

    // Set from the clock
    property real posX: -200 * s
    property real posY: -200 * s
    property real flipX: -1
    property real leanR: -16
    property real leanSX: 1
    property real leanSY: 1
    property real legA: 38
    property real legB: 38
    property real wingR: 0
    property real neckR: 18
    property real headR: -8
    property real billR: 0
    property real noteR: 0
    property real noteO: 0
    property real eyeY: 1
    property real barS: 0.6
    property real barO: 0
    property real cardS: 0.9
    property real cardO: 0
    property real pressN: 1
    property real pressY: 1
    property real wordsO: 0
    property real wordsY: 4
    property var rows: [0, 0, 0, 0]

    // A skip starts from the pose it was in
    property var pose: null
    property real mix: 1
    property real noteIn: 1
    property real blink: 1
    property bool barGone: false

    readonly property real w: win.width

    onTChanged: frame()
    onMixChanged: frame()

    // ---------- the draft's curves and keyframes ----------

    function bezier(x1, y1, x2, y2) {
        const b = (a, c, u) => 3 * a * u * (1 - u) * (1 - u) + 3 * c * u * u * (1 - u) + u * u * u;
        const d = (a, c, u) => 3 * a * (1 - u) * (1 - u) + 6 * c * u * (1 - u) + 3 * u * u;
        return x => {
            if (x <= 0 || x >= 1)
                return x;
            let u = x;
            for (let i = 0; i < 6; i++) {
                const slope = d(x1, x2, u);
                if (Math.abs(slope) < 1e-4)
                    break;
                u -= (b(x1, x2, u) - x) / slope;
            }
            return b(y1, y2, u);
        };
    }

    function at(track, time) {
        if (time <= track[0][0])
            return track[0][1];
        const last = track.length - 1;
        if (time >= track[last][0])
            return track[last][1];
        let i = 0;
        while (track[i + 1][0] <= time)
            i++;
        const a = track[i], b = track[i + 1];
        const u = (time - a[0]) / (b[0] - a[0]);
        return a[1] + (b[1] - a[1]) * (a[2] ? a[2](u) : u);
    }

    function sorted(track) {
        return track.map((key, i) => [key, i]).sort((a, b) => a[0][0] - b[0][0] || a[1] - b[1]).map(pair => pair[0]);
    }

    function flaps(OUT, IO, t0, t1) {
        const o = [[t0, 0, OUT]];
        let time = t0 + 30, beat = 0;
        while (time < t1 - 160) {
            if (beat % 3 === 2) {
                o.push([time, -28]);
                time += 240;
            } else {
                o.push([time, -70, IO]);
                o.push([time + 100, 18, IO]);
                time += 200;
            }
            beat++;
        }
        o.push([t1, 0]);
        return o;
    }

    function keyframes(saidYes) {
        const OUT = bezier(.23, 1, .32, 1), IO = bezier(.77, 0, .175, 1);
        const STEPS = [1400, 1780, 2160, 2540];
        const lean = [[0, -16], [1160, -16, OUT], [1240, 0, OUT, 1.06, .9], [1400, 0], [3000, 0, OUT], [3120, 18], [3200, 18, IO], [3320, -8, OUT], [3520, 0], [3570, 0, OUT, 1.05, .9], [3660, -16], [4390, -16, OUT], [4470, 0, OUT, 1.05, .9], [4640, 0], [7350, 0, OUT], [7410, 0, OUT, 1.05, .9], [7490, -16]];
        const scaled = i => lean.map(key => [key[0], i === 3 ? (key[3] || 1) : (key[4] || 1), key[2]]);
        const legs = side => {
            const fold = [[0, 38], [1120, 38], [1220, 0], [3570, 0], [3680, 38], [4360, 38], [4440, 0], [7400, 0], [7500, 38]];
            STEPS.forEach((step, j) => {
                if (j % 2 === side)
                    fold.push([step, 0, IO], [step + 150, -26, IO], [step + 300, 0]);
            });
            return sorted(fold);
        };
        const bill = [[0, 0]];
        const clatter = (a, b) => {
            for (let time = a; time < b; time += 70)
                bill.push([time, 0], [time + 35, 13]);
            bill.push([b, 0]);
        };
        clatter(2860, 3000);
        if (saidYes)
            clatter(7130, 7400);
        const row = i => {
            const a = 4500 + i * 45;
            return [[0, 0], [a, 0, OUT], [a + 200, 1]];
        };
        const stepping = [];
        STEPS.forEach(step => stepping.push([step, 0, IO], [step + 150, 7, IO], [step + 300, 0]));
        const nodNeck = saidYes ? -28 : 16, nodHead = saidYes ? -24 : 20;
        k = {
            pos: { STEPS: STEPS, IO: IO },
            flip: [[0, -1], [3600, -1, IO], [3720, 1], [4300, 1, IO], [4420, -1], [7370, -1, IO], [7490, 1]],
            leanR: lean.map(key => [key[0], key[1], key[2]]),
            leanSX: scaled(3),
            leanSY: scaled(4),
            legA: legs(0),
            legB: legs(1),
            wing: sorted([...flaps(OUT, IO, 300, 1220), ...flaps(OUT, IO, 3580, 4440), ...flaps(OUT, IO, 7400, 8300)]),
            bill: sorted(bill),
            neck: sorted([[0, 18], [1220, 18, OUT], [1400, 0], ...stepping, [3000, 0, OUT], [3120, 55], [3200, 55, IO], [3320, -12, OUT], [3520, 0], [3570, 18], [4390, 18, OUT], [4560, 0], [7120, 0, OUT], [7240, nodNeck], [7350, nodNeck, OUT], [7420, 18]]),
            head: [[0, -8], [1400, 4], [2860, 4, IO], [2900, -22], [3000, -22, OUT], [3120, 14], [3320, 0], [4500, 10], [7120, 10, OUT], [7260, nodHead], [7400, 0]],
            noteO: sorted([[0, 0], [3315, 0], [3325, 1], [4460, 1], [4490, 0], ...(saidYes ? [[7200, 0], [7270, 1]] : [])]),
            noteR: sorted([[0, 0], [3315, 0], [3325, -22, OUT], [3700, 16, IO], [4100, -14, IO], [4460, 0], ...(saidYes ? [[7270, 0, IO], [7700, -12, IO], [8300, 8]] : [])]),
            eye: [[2090, 1], [2140, .1], [2210, 1]],
            barS: [[3000, .6, OUT], [3020, .6, OUT], [3200, .8, IO], [3310, 2.5], [3320, 2.6]],
            barO: [[3000, 0], [3010, 1], [3310, 1], [3320, 0]],
            cardS: [[4440, .9, OUT], [4680, 1], [7120, 1, OUT], [7290, .93]],
            cardO: [[4440, 0, OUT], [4680, 1], [7120, 1, OUT], [7290, 0]],
            rows: [row(0), row(1), row(2), row(3)],
            press: [[6920, 1, OUT], [7000, .95, OUT], [7120, 1]],
            wordsO: [[7160, 0, OUT], [7320, 1], [7620, 1], [7760, 0]],
            wordsY: [[7160, 4, OUT], [7320, 0]]
        };
    }

    function flown(time, t0, t1, x0, y0, x1, y1, arc) {
        const u = (time - t0) / (t1 - t0);
        const p = u < .5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2;
        posX = x0 + (x1 - x0) * p;
        posY = y0 + (y1 - y0) * p - arc * s * Math.sin(p * Math.PI);
    }

    function place(time) {
        if (time <= 1220) {
            flown(time, 300, 1220, inX, inY, walkX, pillTop, -26);
        } else if (time < 3600) {
            let x = walkX;
            k.pos.STEPS.forEach((step, i) => {
                if (time >= step)
                    x = walkX - stepX * (i + (time >= step + 300 ? 1 : k.pos.IO((time - step) / 300)));
            });
            posX = x;
            posY = pillTop;
        } else if (time <= 4440) {
            flown(time, 3600, 4440, fromX, fromY, perchX, perchY, 60);
        } else if (time <= 7400) {
            posX = perchX;
            posY = perchY;
        } else {
            flown(time, 7400, 8300, perchX, perchY, outX, outY, 10);
        }
    }

    function blended(name, value) {
        return pose ? pose[name] + (value - pose[name]) * mix : value;
    }

    function frame() {
        if (!k.flip)
            return;
        place(t);
        flipX = blended("flipX", at(k.flip, t));
        leanR = blended("leanR", at(k.leanR, t));
        leanSX = at(k.leanSX, t);
        leanSY = at(k.leanSY, t);
        legA = at(k.legA, t);
        legB = at(k.legB, t);
        wingR = at(k.wing, t);
        neckR = blended("neckR", at(k.neck, t));
        headR = blended("headR", at(k.head, t));
        billR = at(k.bill, t);
        noteR = blended("noteR", at(k.noteR, t));
        noteO = at(k.noteO, t) * noteIn;
        eyeY = at(k.eye, t);
        barS = at(k.barS, t);
        barO = barGone ? 0 : at(k.barO, t);
        cardS = at(k.cardS, t);
        cardO = at(k.cardO, t);
        rows = k.rows.map(track => at(track, t));
        const press = quiet ? 1 : at(k.press, t);
        pressY = yes ? press : 1;
        pressN = yes ? 1 : press;
        wordsO = quiet ? 0 : at(k.wordsO, t);
        wordsY = at(k.wordsY, t);
    }

    // ---------- what happens ----------

    function open() {
        keyframes(true);
        inX = win.width + 60 * s;
        inY = Math.max(-60 * s, pillY - 382 * s);
        outX = win.width + 60 * s;
        outY = Math.max(-80 * s, perchY - 192 * s);
        fromX = walkX - stepX * 4;
        fromY = pillTop;
        const far = p => Math.min(2, Math.max(1, p));
        arriveMs = 920 * far(Math.hypot(inX - walkX, inY - pillTop) / (420 * s));
        leaveMs = 900 * far(Math.hypot(outX - perchX, outY - perchY) / (420 * s));
        phase = "playing";
        frame();
        if (shell.active && !shell.atTop)   // at the top of the screen there is no room to stand on the pill
            intro.restart();
        else
            toCard();
    }

    // The pill closed, or never came: no pill to walk on, so straight to the card from wherever it is.
    function toCard() {
        intro.stop();
        pose = { flipX: flipX, leanR: leanR, neckR: neckR, headR: headR, noteR: noteR };
        fromX = posX;
        fromY = posY;
        barGone = true;
        mix = 0;
        noteIn = 0;
        t = 3560;
        skip.restart();
    }

    function ready() {
        phase = "waiting";
        giveUp.restart();
    }

    function goOn(from) {
        giveUp.stop();
        blinks.stop();
        blink = 1;
        phase = "leaving";
        keyframes(yes);
        resumeAt = from;
        t = from;
        outro.restart();
    }

    function answer(saidYes) {
        if (phase !== "waiting")
            return;
        yes = saidYes;
        send.what = saidYes ? "yes" : "no";
        send.running = true;
        notice.answered();
        goOn(answerAt);
    }

    // Answered elsewhere, or no longer wanted.
    function leave() {
        if (phase === "leaving" || phase === "")
            return;
        intro.stop();
        skip.stop();
        giveUp.stop();
        phase = "leaving";
        fade.restart();
    }

    Connections {
        target: notice.shell
        function onActiveChanged() {
            if (!notice.shell.active && notice.phase === "playing" && notice.t < 3320 && intro.running)
                notice.toCard();
        }
    }

    Timer {
        // a new layer surface has no size until the compositor answers; the pill comes a moment after the event
        interval: 16
        repeat: true
        running: notice.phase === "" && win.width > 0 && win.height > 0
        onTriggered: {
            notice.tries++;
            if (notice.shell.active || notice.tries > 30)
                notice.open();
        }
    }

    Timer {
        id: giveUp  // no answer: gone for now, and back with the next dictation
        interval: 30000
        onTriggered: {
            notice.quiet = true;
            notice.goOn(notice.quietAt);
        }
    }

    Timer {
        // the one thing that moves while it waits
        interval: 3600
        repeat: true
        running: notice.phase === "waiting"
        onTriggered: blinks.restart()
    }

    Process {
        id: send
        property string what: ""
        command: {
            const python = Quickshell.env("STENTOR_PYTHON");
            return (python ? [python, "-m", "stentor"] : ["stentor"]).concat(["consent", what]);
        }
    }

    Process {
        id: info
        command: {
            const python = Quickshell.env("STENTOR_PYTHON");
            return (python ? [python, "-m", "stentor"] : ["stentor"]).concat(["consent", "info"]);
        }
    }

    Region {
        id: noInput
    }
    Region {
        id: hitInput
        item: card
    }

    SequentialAnimation {
        id: intro
        NumberAnimation {
            target: notice
            property: "t"
            from: 300
            to: 1220
            duration: notice.arriveMs
        }
        NumberAnimation {
            target: notice
            property: "t"
            to: notice.waitAt
            duration: notice.waitAt - 1220
        }
        ScriptAction {
            script: notice.ready()
        }
    }

    ParallelAnimation {
        id: skip
        NumberAnimation {
            target: notice
            property: "t"
            to: notice.waitAt
            duration: notice.waitAt - 3560
        }
        NumberAnimation {
            target: notice
            property: "mix"
            to: 1
            duration: 220
            easing.type: Easing.OutCubic
        }
        NumberAnimation {
            target: notice
            property: "noteIn"
            to: 1
            duration: 180
        }
        onFinished: notice.ready()
    }

    SequentialAnimation {
        id: outro
        NumberAnimation {
            target: notice
            property: "t"
            to: 7400
            duration: 7400 - notice.resumeAt
        }
        NumberAnimation {
            target: notice
            property: "t"
            to: 8300
            duration: notice.leaveMs
        }
        ScriptAction {
            script: notice.finished()
        }
    }

    SequentialAnimation {
        id: fade
        NumberAnimation {
            target: scene
            property: "opacity"
            to: 0
            duration: 200
        }
        ScriptAction {
            script: notice.finished()
        }
    }

    SequentialAnimation {
        id: blinks
        NumberAnimation {
            target: notice
            property: "blink"
            to: 0.1
            duration: 50
        }
        NumberAnimation {
            target: notice
            property: "blink"
            to: 1
            duration: 70
        }
    }

    component Art: Shape {
        id: art
        property string d
        property color fill: "transparent"
        property color line: "transparent"
        property real lineWidth: 0
        property int cap: ShapePath.FlatCap
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            fillColor: art.fill
            strokeColor: art.line
            strokeWidth: art.lineWidth
            capStyle: art.cap
            joinStyle: ShapePath.RoundJoin
            PathSvg {
                path: art.d
            }
        }
    }

    component Answer: Rectangle {
        id: button
        property string label
        property bool primary
        property real pressed: 1
        signal chosen()
        width: 94 * notice.s
        height: 34 * notice.s
        radius: 10 * notice.s
        color: primary ? (area.containsMouse ? "#f4b67e" : "#f0a764") : (area.containsMouse ? "#3e3d47" : "#32313a")
        scale: pressed
        Text {
            anchors.centerIn: parent
            text: button.label
            color: button.primary ? "#17161a" : "#efece6"
            font.pixelSize: 12.5 * notice.s
            font.weight: Font.DemiBold
        }
        MouseArea {
            id: area
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: button.chosen()
        }
    }

    PanelWindow {
        id: win
        visible: true
        color: "transparent"
        anchors.top: true
        anchors.bottom: true
        anchors.left: true
        anchors.right: true
        exclusionMode: ExclusionMode.Ignore   // it has to line up with the pill
        mask: notice.phase === "waiting" ? hitInput : noInput   // clicks pass through everything else
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "stentor-consent"

        Item {
            id: scene
            anchors.fill: parent

            // The bar it plucks, over the real bars of the pill.
            Rectangle {
                x: notice.pillX + 16 * notice.s - width / 2
                y: notice.pillY - height / 2
                width: 4 * notice.s
                height: 30 * notice.s
                radius: width / 2
                color: notice.shell.barColor
                opacity: notice.barO * notice.shell.shown
                transform: Scale {
                    origin.y: 15 * notice.s
                    yScale: notice.barS
                }
            }

            Rectangle {
                id: card
                x: notice.cardX
                y: notice.cardY
                width: notice.cardW
                height: notice.cardH
                radius: 15 * notice.s
                color: "#1d1c21"
                border.width: 1
                border.color: "#17ffffff"
                opacity: notice.cardO
                transform: Scale {
                    origin.x: 54 * notice.s
                    xScale: notice.cardS
                    yScale: notice.cardS
                }

                Text {
                    x: 16 * notice.s
                    y: 21 * notice.s + (1 - notice.rows[0]) * 5 * notice.s
                    opacity: notice.rows[0]
                    text: "Help improve accuracy?"
                    color: "#f6f3ed"
                    font.pixelSize: 16 * notice.s
                    font.weight: Font.DemiBold
                }
                Text {
                    id: body
                    x: 16 * notice.s
                    y: 48 * notice.s + (1 - notice.rows[1]) * 5 * notice.s
                    width: parent.width - 32 * notice.s
                    opacity: notice.rows[1]
                    text: "Save your recordings and their text on our servers to train our models. Change it anytime in Settings."
                    color: "#b6b2ab"
                    font.pixelSize: 12.5 * notice.s
                    lineHeight: 18 * notice.s
                    lineHeightMode: Text.FixedHeight
                    wrapMode: Text.WordWrap
                }
                Text {
                    id: more
                    x: 16 * notice.s
                    y: 48 * notice.s + body.contentHeight + 7 * notice.s + (1 - notice.rows[2]) * 5 * notice.s
                    opacity: notice.rows[2]
                    text: "What we collect"
                    color: "#f0a764"
                    font.pixelSize: 12 * notice.s
                    font.underline: moreArea.containsMouse
                    MouseArea {
                        id: moreArea
                        anchors.fill: parent
                        anchors.margins: -6 * notice.s
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: info.running = true
                    }
                }
                Answer {
                    x: 16 * notice.s
                    y: parent.height - height - 13 * notice.s + (1 - notice.rows[3]) * 5 * notice.s
                    opacity: notice.rows[3]
                    label: "No thanks"
                    primary: false
                    pressed: notice.pressN
                    onChosen: notice.answer(false)
                }
                Answer {
                    x: parent.width - width - 16 * notice.s
                    y: parent.height - height - 13 * notice.s + (1 - notice.rows[3]) * 5 * notice.s
                    opacity: notice.rows[3]
                    label: "Yes, help"
                    primary: true
                    pressed: notice.pressY
                    onChosen: notice.answer(true)
                }
            }

            // The stork. Every item sits at a pivot of the draft, in the draft's units.
            Item {
                x: notice.posX
                y: notice.posY
                transform: Scale {
                    xScale: notice.flipX * notice.s
                    yScale: notice.s
                }

                Item {
                    transform: [
                        Scale {
                            xScale: notice.leanSX
                            yScale: notice.leanSY
                        },
                        Rotation {
                            angle: notice.leanR
                        }
                    ]

                    Item {
                        x: -5
                        y: -17
                        transform: Rotation {
                            angle: notice.legA
                        }
                        Art {
                            d: "M0 0L1.2 9L-.6 17H5"
                            line: "#e4572e"
                            lineWidth: 2
                            cap: ShapePath.RoundCap
                        }
                    }
                    Item {
                        x: 5
                        y: -17
                        transform: Rotation {
                            angle: notice.legB
                        }
                        Art {
                            d: "M0 0L1.2 9L-.6 17H5"
                            line: "#e4572e"
                            lineWidth: 2
                            cap: ShapePath.RoundCap
                        }
                    }

                    Art {
                        d: "M-12-20 L-37-12 L-35-6 L-8-13Z"
                        fill: "#f3f1ec"
                    }
                    Art {
                        d: "M-24-24 C-18-38 0-44 12-41 C20-36 15-20 2-17 C-10-15-22-16-24-24Z"
                        fill: "#f6f4ef"
                    }
                    Art {
                        d: "M-16-17 C-6-14 8-18 14-30 C16-23 10-17 2-17 C-6-15.5-12-16-16-17Z"
                        fill: "#d8d6d4"
                    }

                    Item {
                        x: 2
                        y: -34
                        transform: Rotation {
                            angle: notice.wingR
                        }
                        Art {
                            d: "M6 0 C-6-9-24-7-30 1 C-20 11-6 11 6 6Z"
                            fill: "#fbfaf6"
                        }
                        Art {
                            d: "M-14-4.6 C-20-5-26-3-30 1 L-48 8 L-40 9 L-44 13 L-35 12.5 L-36 16 C-27 13-20 11-13 9Z"
                            fill: "#1e1d23"
                        }
                    }

                    Item {
                        x: 8
                        y: -38
                        transform: Rotation {
                            angle: notice.neckR
                        }
                        Art {
                            d: "M0 0 C12-7 3-18 8-30"
                            line: "#f6f4ef"
                            lineWidth: 8
                            cap: ShapePath.RoundCap
                        }

                        Item {
                            x: 8
                            y: -31
                            transform: Rotation {
                                angle: notice.headR
                            }
                            Rectangle {
                                x: -7.6
                                y: -6.6
                                width: 15.2
                                height: 13.2
                                radius: 6.6
                                color: "#f6f4ef"
                            }
                            Item {
                                x: 5
                                y: 1
                                transform: Rotation {
                                    angle: notice.billR
                                }
                                Art {
                                    d: "M0-1 L31 2.4 L0 2.6Z"
                                    fill: "#c9431f"
                                }
                            }
                            Art {
                                d: "M5-3.4 L37 3.2 L5 1.6Z"
                                fill: "#e8582f"
                            }
                            Item {
                                x: 2
                                y: -1.8
                                transform: Scale {
                                    yScale: Math.min(notice.eyeY, notice.blink)
                                }
                                Rectangle {
                                    x: -2.4
                                    y: -2.4
                                    width: 4.8
                                    height: 4.8
                                    radius: 2.4
                                    color: "#c9431f"
                                }
                                Rectangle {
                                    x: -1.5
                                    y: -1.5
                                    width: 3
                                    height: 3
                                    radius: 1.5
                                    color: "#121116"
                                }
                                Rectangle {
                                    x: 0.05
                                    y: -1.05
                                    width: 0.9
                                    height: 0.9
                                    radius: 0.45
                                    color: "#ffffff"
                                }
                            }

                            // The envelope, on two strings from the bill
                            Item {
                                x: 34
                                y: 3
                                opacity: notice.noteO
                                transform: Rotation {
                                    angle: notice.noteR
                                }
                                Art {
                                    d: "M-4 0 L-3.5 9 M2 0 L8 9"
                                    line: "#f4f1ea"
                                    lineWidth: 1.2
                                }
                                Item {
                                    x: -8
                                    y: 9
                                    Rectangle {
                                        width: 21
                                        height: 14
                                        radius: 2
                                        color: "#fbf8f1"
                                    }
                                    Art {
                                        d: "M.8 .8 L10.5 8 L20.2 .8"
                                        line: "#b6ad9d"
                                        lineWidth: 1.3
                                    }
                                    Rectangle {
                                        x: 8.6
                                        y: 6.1
                                        width: 3.8
                                        height: 3.8
                                        radius: 1.9
                                        color: "#e0705c"
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // What it says as it goes. A chip, because the screen behind it can be any colour.
            Rectangle {
                x: Math.min(notice.perchX + 50 * notice.s - width / 2, notice.w - width - 12)
                y: Math.max(8, notice.perchY - 112 * notice.s) + notice.wordsY * notice.s
                width: words.implicitWidth + 22 * notice.s
                height: words.implicitHeight + 10 * notice.s
                radius: height / 2
                color: "#1d1c21"
                opacity: notice.wordsO
                Text {
                    id: words
                    anchors.centerIn: parent
                    text: notice.yes ? "Thank you!" : "Got it"
                    color: "#ffffff"
                    font.pixelSize: 13 * notice.s
                    font.weight: Font.DemiBold
                }
            }
        }
    }
}
