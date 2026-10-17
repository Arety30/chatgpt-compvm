# chatgpt-compvm: a lean, disposable Ubuntu "computer" — X desktop + Firefox +
# terminal, driven over VNC (for the live /monitor view) and xdotool/scrot
# (for ChatGPT's click/type/screenshot tools).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/root \
    DISPLAY=:99 \
    LANG=C.UTF-8

# Desktop stack + automation tools. Firefox comes from Mozilla's own APT repo
# (the Ubuntu 24.04 "firefox" package is a snap, which cannot run in a container).
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc openbox xterm xfce4-terminal xcape scrot xdotool wmctrl xclip \
        x11-xserver-utils \
        lxpanel lxmenu-data pcmanfm plank rofi \
        yaru-theme-gtk yaru-theme-icon fonts-ubuntu xsettingsd feh \
        gnome-calculator gedit \
        dbus-x11 x11-utils procps psmisc \
        ca-certificates wget curl git nano less \
        fonts-liberation fonts-dejavu-core libgtk-3-0 libasound2t64 \
    && install -d -m 0755 /etc/apt/keyrings \
    && wget -qO- https://packages.mozilla.org/apt/repo-signing-key.gpg \
        > /etc/apt/keyrings/packages.mozilla.org.asc \
    && echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
        > /etc/apt/sources.list.d/mozilla.list \
    && printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' \
        > /etc/apt/preferences.d/mozilla \
    && apt-get update && apt-get install -y --no-install-recommends firefox \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Desktop configuration (Yaru theme, lxpanel Raspberry-Pi-style menu, rofi grid,
# openbox keybinds/menu). Baked in so a disposable rebuild reproduces the look.
COPY rootfs/ /

COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

# 5900 = raw VNC (bound to host localhost only; reached solely through the
# token-gated websocket bridge in the host MCP server).
EXPOSE 5900
ENTRYPOINT ["/usr/local/bin/start.sh"]
