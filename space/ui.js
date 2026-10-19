() => {
    // Own both scroll surfaces: Gradio's chat viewport and the surrounding page.
    // No model/tool calls or persisted learner data are involved.
    window.__coachScrollCleanup?.();
    const install = () => {
        const chat = document.getElementById("coach-chat");
        const panel = document.getElementById("coach-panel");
        const choices = document.getElementById("coach-choices");
        const composer = document.getElementById("coach-composer");
        if (!chat || !panel || !composer) return;

        const events = new AbortController();
        const listen = (target, name, callback, options = {}) =>
            target?.addEventListener(name, callback, {...options, signal: events.signal});
        let followChat = true;
        let followPage = true;
        let frame = 0;
        let lastPageTop = window.scrollY;
        let userScrollUntil = 0;
        let dragging = false;
        let touchY = 0;
        const userScrolling = () => dragging || performance.now() < userScrollUntil;
        const pause = inChat => {
            if (inChat) followChat = false;
            followPage = false;
            updateButton();
        };
        let log = null;
        let lastChatTop = 0;
        const atEnd = node => node.scrollHeight - node.clientHeight - node.scrollTop <= 80;
        const composerInView = () => {
            const bottom = composer.getBoundingClientRect().bottom;
            return bottom >= 0 && bottom <= window.innerHeight + 80;
        };

        const latest = document.createElement("button");
        latest.type = "button";
        latest.textContent = "↓ 回到最新";
        latest.setAttribute("aria-label", "回到最新消息并继续自动跟随");
        latest.id = "coach-latest";
        latest.hidden = true;
        latest.style.cssText = "position:fixed;right:24px;bottom:20px;z-index:100;" +
            "border:1px solid #c7d8e3;border-radius:999px;padding:10px 16px;" +
            "background:#fff;color:#17384b;box-shadow:0 2px 12px #0002;cursor:pointer;";
        document.body.append(latest);
        const updateButton = () => { latest.hidden = followChat && followPage; };

        function schedule() {
            if (frame) return;
            frame = requestAnimationFrame(() => {
                frame = 0;
                if (followChat && log) {
                    log.scrollTop = log.scrollHeight;
                    lastChatTop = log.scrollTop;
                }
                if (followPage) {
                    const bottom = composer.getBoundingClientRect().bottom;
                    // Keep the next action visible, without jumping up from notes below it.
                    if (bottom > window.innerHeight - 16) {
                        window.scrollTo({top: window.scrollY + bottom - window.innerHeight + 16,
                                         behavior: "instant"});
                    }
                    lastPageTop = window.scrollY;
                }
                updateButton();
            });
        }

        const resize = new ResizeObserver(schedule);
        function bindLog() {
            const next = chat.querySelector('[role="log"]');
            if (next !== log) {
                log = next;
                lastChatTop = log?.scrollTop || 0;
                listen(log, "scroll", () => {
                    if (userScrolling() && log.scrollTop < lastChatTop - 2) {
                        followChat = false;
                        followPage = false;
                    } else if (userScrolling() && atEnd(log)) {
                        followChat = true;
                        if (composerInView()) followPage = true;
                    }
                    lastChatTop = log.scrollTop;
                    updateButton();
                }, {passive: true});
            }
            // Inner content can grow while the fixed-height Chatbot does not resize.
            resize.disconnect();
            [panel, choices, composer, ...(log ? [log, ...log.children] : [])]
                .filter(Boolean).forEach(node => resize.observe(node));
        }
        const changes = new MutationObserver(() => { bindLog(); schedule(); });
        [chat, panel, choices].filter(Boolean).forEach(node =>
            changes.observe(node, {childList: true, subtree: true, characterData: true}));
        bindLog();
        listen(chat, "load", schedule, {capture: true}); // Late-loading inline images.
        listen(panel, "toggle", schedule, {capture: true}); // Native details close/reopen.
        listen(window, "resize", schedule, {passive: true});
        listen(window, "scroll", () => {
            if (userScrolling() && window.scrollY < lastPageTop - 2) followPage = false;
            else if (userScrolling() && composerInView()) followPage = true;
            lastPageTop = window.scrollY;
            updateButton();
        }, {passive: true});
        // Layout changes (closing a tall visual, rendering the final reply) can
        // move scrollTop too. Only a real user gesture may suspend following.
        listen(document, "wheel", event => {
            userScrollUntil = performance.now() + 1000;
            if (event.deltaY < 0) pause(chat.contains(event.target));
        }, {passive: true, capture: true});
        listen(document, "touchstart", event => { touchY = event.touches[0]?.clientY || 0; }, {passive: true});
        listen(document, "touchmove", event => {
            const y = event.touches[0]?.clientY || 0;
            userScrollUntil = performance.now() + 1000;
            if (y > touchY + 2) pause(chat.contains(event.target));
            touchY = y;
        }, {passive: true});
        listen(document, "pointerdown", event => {
            dragging = !event.target.closest("button, summary, input, textarea, [contenteditable=true]");
        }, {capture: true});
        for (const name of ["pointerup", "pointercancel"]) {
            listen(document, name, () => {
                if (dragging) userScrollUntil = performance.now() + 300;
                dragging = false;
            }, {capture: true});
        }
        listen(document, "keydown", event => {
            if (event.target.closest("input, textarea, [contenteditable=true]")) return;
            if (["ArrowUp", "PageUp", "Home"].includes(event.key) || (event.key === " " && event.shiftKey)) {
                userScrollUntil = performance.now() + 1000;
                pause(chat.contains(event.target));
            } else if (["ArrowDown", "PageDown", "End", " "].includes(event.key)) {
                userScrollUntil = performance.now() + 1000;
            }
        });

        const resume = () => {
            userScrollUntil = 0;
            dragging = false;
            followChat = true;
            followPage = true;
            schedule();
        };
        listen(latest, "click", () => {
            resume();
            // The page may be below the conversation, in the learning notes.
            composer.scrollIntoView({block: "end", behavior: "instant"});
            lastPageTop = window.scrollY;
        });
        ["coach-send", "coach-reset", "coach-clear"].forEach(id =>
            listen(document.getElementById(id), "click", resume, {capture: true}));
        listen(choices, "click", event => { if (event.target.closest("button")) resume(); }, {capture: true});
        listen(composer, "keydown", event => {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) resume();
        }, {capture: true});

        window.__coachScrollCleanup = () => {
            events.abort();
            changes.disconnect();
            resize.disconnect();
            cancelAnimationFrame(frame);
            latest.remove();
        };
    };
    // With an invite key, Gradio mounts the main area only after check_access.
    const ready = () => document.getElementById("coach-chat") &&
        document.getElementById("coach-panel") && document.getElementById("coach-composer");
    if (ready()) install();
    else {
        const mounted = new MutationObserver(() => {
            if (ready()) { mounted.disconnect(); install(); }
        });
        mounted.observe(document.body, {childList: true, subtree: true});
        window.__coachScrollCleanup = () => mounted.disconnect();
    }
}
