#!/bin/bash
# Entrypoint: bring up an Ubuntu-style desktop (openbox + Yaru theme + lxpanel
# Start menu + plank dock), VNC, a terminal and Firefox. Firefox uses a profile
# on the persistent /data volume so logins survive the container being destroyed
# and recreated (that's what makes the VM "disposable" without losing sessions).
set -e

export DISPLAY=:99
export HOME=/root
export GTK_THEME=Yaru-dark
export XCURSOR_THEME=Yaru
SCREEN="${SCREEN:-1440x900x24}"
PROFILE=/data/firefox-profile
WORKSPACE=/data/workspace

mkdir -p "$PROFILE" "$WORKSPACE"
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

# Session bus so Firefox / GTK apps behave.
eval "$(dbus-launch --sh-syntax)"
export DBUS_SESSION_BUS_ADDRESS DBUS_SESSION_BUS_PID

# Virtual framebuffer.
Xvfb :99 -screen 0 "$SCREEN" -ac +extension RANDR +extension GLX >/var/log/xvfb.log 2>&1 &
for i in $(seq 1 30); do xdpyinfo -display :99 >/dev/null 2>&1 && break; sleep 0.3; done

# Dark aubergine desktop background.
xsetroot -solid "#2c001e" 2>/dev/null || true

# Window manager (config: /root/.config/openbox, baked into the image).
openbox >/var/log/openbox.log 2>&1 &
sleep 1

# Theme daemon (Yaru-dark + Yaru icons + Ubuntu font for all GTK apps).
xsettingsd -c /root/.config/xsettingsd/xsettingsd.conf >/var/log/xsettingsd.log 2>&1 &

# Top panel with the Raspberry-Pi-style categorised Start menu + taskbar.
lxpanel --profile default >/var/log/lxpanel.log 2>&1 &
# Bottom dock.
plank >/var/log/plank.log 2>&1 &
# Win-key tap -> app-grid launcher (rofi), via openbox binding on XF86LaunchB.
xcape -e 'Super_L=XF86LaunchB' >/var/log/xcape.log 2>&1 &

# VNC server. -nopw is safe: 5900 is published only on the host's 127.0.0.1 and
# is reachable exclusively through the MCP server's token-gated /monitor bridge.
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 \
       -noxdamage -ncache 0 -o /var/log/x11vnc.log -bg

# Firefox uses /data/firefox-profile as its DEFAULT profile (via the baked
# ~/.mozilla/firefox/profiles.ini), so EVERY launch method — start.sh, the dock,
# the Start menu, rofi — opens your one logged-in profile. No --no-remote, so a
# second launch just opens a window in the running session instead of a blank
# profile. Session-restore brings tabs back after any restart.
[ -f "$PROFILE/user.js" ] || cat > "$PROFILE/user.js" <<'JS'
user_pref("browser.startup.page", 3);
user_pref("browser.sessionstore.resume_from_crash", true);
user_pref("browser.aboutwelcome.enabled", false);
JS

# An Ubuntu terminal + Firefox, maximised.
xfce4-terminal --working-directory="$WORKSPACE" --geometry=110x28+40+60 \
      --title="Ubuntu Terminal" >/var/log/terminal.log 2>&1 &
firefox >/var/log/firefox.log 2>&1 &
( sleep 6; wmctrl -r :ACTIVE: -b add,maximized_vert,maximized_horz 2>/dev/null ) &

echo "chatgpt-compvm desktop up on :99 ($SCREEN)"
exec tail -f /var/log/x11vnc.log
